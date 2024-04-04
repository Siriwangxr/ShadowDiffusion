import os
import cv2
import pylab as pl
import numpy as np
import torch
from PIL import Image
from scipy.io._idl import AttrDict
from torch import nn
import argparse
import logging
import hydra
import core.logger as Logger
import torch.nn.functional as F
from torchvision import transforms
from torch.utils.data import DataLoader
from model.sam_adapter.sam_adapt import SAM
import core.metrics as Metrics
from data.LRHR_dataset import LRHRDataset
import lightning as L
from lightning.pytorch.callbacks import ModelCheckpoint
from lightning.pytorch.strategies import DDPStrategy
from omegaconf import DictConfig
import model.networks as networks
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.loggers import TensorBoardLogger


# define a LightningModule, in which I have shadowDiffusion and SAM_adapter two models
class sam_shadow(L.LightningModule):
    def __init__(self, sam_adapter, shadowdiffusion, args, path=None):
        super().__init__()
        self.diffusion = shadowdiffusion(args)
        self.sam = sam_adapter(args.sam.input_size, args.sam.loss)
        self.lr = 3e-5
        if path is not None:
            self.load_pretrained_models(path.ddpm, path.sam)
        self.save_hyperparameters()


        # freeze sam
        for param in self.sam.parameters():
            param.requires_grad = False

    def training_step(self, train_data, batch_idx):

        for param in self.sam.parameters():
            if param.requires_grad:
                print("sam is not freeze")

        inp = train_data['SR']
        gt = train_data['mask']

        inp = F.interpolate(inp, size=(1024, 1024), mode='bilinear', align_corners=False)
        gt = F.interpolate(gt, size=(1024, 1024), mode='bilinear', align_corners=False)

        sam_pred_mask = self.sam(inp)
        # sam_pred_mask = F.interpolate(sam_pred_mask, size=(256, 256), mode='bilinear', align_corners=False)
        save_mask = sam_pred_mask[0].detach().cpu().numpy()
        save_mask = np.squeeze((save_mask * 255).astype(np.uint8))
        cv2.imwrite('mask.png', save_mask)

        train_data['mask'] = sam_pred_mask
        l_pix = self.diffusion.netG(train_data)
        b, c, h, w = self.diffusion.data['HR'].shape
        l_pix = l_pix.sum() / int(b * c * h * w)

        return l_pix

    def load_pretrained_models(self, path):
        # ddpm_state_dict = torch.load(ddpm_checkpoint_path)
        self.diffusion.load_network(path.ddpm)

        sam_state_dict = torch.load(path.sam)
        self.sam.load_state_dict(sam_state_dict, strict=False)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        return optimizer

# define the shadow_diffusion model
class diffusion(L.LightningModule):
    def __init__(self, shadowdiffusion, args, path=None):
        super().__init__()
        self.args = args
        self.diffusion = shadowdiffusion(args)
        self.optimizer_param = args.train.optimizer
        self.diffusion.set_loss() # l1 loss and reduction='sum'
        if args.phase == 'train':
            self.diffusion.set_new_noise_schedule(args['model']['beta_schedule']['train'])
        if path is not None:
            self.load_pretrained_models(path)
            # load the pretrain model training step and epoch
            if args.phase == 'train':
                # optimizer
                opt_path = '{}_opt.pth'.format(path.ddpm)
                opt = torch.load(opt_path)
                self.begin_step = opt['iter']
                self.begin_epoch = opt['epoch']

        self.save_hyperparameters()
        self.save_model_name = args.name
        self.PSNR = []
        self.SSIM = []
    def training_step(self, train_data, batch_idx):

        l_pix = self.diffusion(train_data)
        b, c, h, w = train_data['HR'].shape
        l_pix = l_pix.sum() / int(b * c * h * w)
        # self.wandb_logger.log({'l_pix': l_pix.item()})
        self.log('l_pix_loss', l_pix.item(), on_step=True)
        return l_pix

    def test_step(self, test_data, batch_idx):
        filename = test_data['LR_path'][0].split('/')[-1].split('.')[0]
        val_data = {key: val for key, val in test_data.items() if key != "LR_path"}
        self.SR, self.mask_pred = self.diffusion.super_resolution(val_data['SR'], val_data['mask'], continous=False)

        # calculate PSNR and SSIM here
        res = Metrics.tensor2img(self.SR)
        hr_img = Metrics.tensor2img(val_data['HR'])
        eval_psnr = Metrics.calculate_psnr(res, hr_img)
        eval_ssim = Metrics.calculate_ssim(res, hr_img)
        self.PSNR.append(eval_psnr)
        self.SSIM.append(eval_ssim)

        self.log(f'{filename}_PSNR', eval_psnr)
        self.log(f'{filename}_SSIM', eval_ssim)

        # save SR and mask_pred
        save_path = f'./experiments/{self.args.name}/{self.args.name}_{self.args.version}/results/'
        if not os.path.exists(save_path):
            os.mkdir(save_path)
        sr_path = os.path.join(save_path, f'{filename}_sr.png')
        sr_img = Image.fromarray(res)
        sr_img.save(sr_path)
        mask = self.mask_pred.squeeze().cpu().numpy() * 255
        mask_path = os.path.join(save_path, f'{filename}_mask.png')
        mask_img = Image.fromarray(mask.astype(np.uint8))
        mask_img.save(mask_path)

    def on_test_end(self):
        avg_psnr = np.mean(self.PSNR)
        avg_ssim = np.mean(self.SSIM)
        print(f'Average PSNR: {avg_psnr}')
        print(f'Average SSIM: {avg_ssim}')


    def load_pretrained_models(self, path):
        gen_path = '{}_gen.pth'.format(path.ddpm)
        self.diffusion.load_state_dict(torch.load(gen_path), strict=False)

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.diffusion.parameters(), lr=self.args.train.optimizer.lr)
        return optimizer

class SamshadowDataModule(L.LightningDataModule):
    def __init__(self, args):
        super().__init__()
        self.num_workers = args.datasets.train.num_workers
        self.shuffle = args.datasets.train.use_shuffle
        self.args = args

    def setup(self, stage="fit"):
        if stage == "fit":
            self.train_data = LRHRDataset(self.args.datasets.train.dataroot, datatype='img', l_resolution='low', r_resolution='high')
        elif stage == "test":
            self.test_data = LRHRDataset(self.args.datasets.test.dataroot, data_len=-1, datatype='img', l_resolution='test_low', r_resolution='test_high',
                                         split='test', need_LR=True)

    def train_dataloader(self):
        self.train_dataloader = DataLoader(self.train_data,
                                           batch_size=self.args.datasets.train.batch_size,
                                           shuffle=self.shuffle,
                                           num_workers=self.num_workers,
                                           pin_memory=True)
        return self.train_dataloader

    def test_dataloader(self):
        self.test_dataloader = DataLoader(self.test_data, batch_size=self.args.datasets.test.batch_size, shuffle=False, num_workers=0, pin_memory=True)
        return self.test_dataloader

# using SAM mask train diffusion
def train(args: DictConfig) -> None:
    logger = TensorBoardLogger(save_dir=f"./experiments_lightning/{args.name}", name=args.name+"_"+args.version+"_logger")
    data_module = SamshadowDataModule(args)
    data_module.setup("fit")
    ckpt_path = args.ckpt_path
    model = diffusion(networks.define_G, args, ckpt_path)

    # load training parameters
    save_model_name = args.name
    max_epochs = args.train.max_epochs
    save_every_n_epochs = args.train.every_n_epochs
    log_every_n_steps = 1

    save_path = f"./experiments_lightning/{save_model_name}/{args.name}_{args.version}"

    checkpoint_callback = ModelCheckpoint(
        dirpath=save_path,
        filename='{args.name}_{args.version}_{epoch}',
        save_top_k=-1,
        every_n_epochs=save_every_n_epochs,  # Save every 20 epochs
        save_last=True,  # Save the last model as well
    )
    trainer = L.Trainer(
        accelerator='gpu',
        devices=args.train.gpu_ids,
        max_epochs=max_epochs,
        default_root_dir=save_path,
        callbacks = [checkpoint_callback],
        logger=logger,
        log_every_n_steps=log_every_n_steps,
        strategy = DDPStrategy(gradient_as_bucket_view=True)
    )
    trainer.fit(model, data_module)


def sample(args: DictConfig) -> None:
    logger = TensorBoardLogger(save_dir=f"./experiments_lightning", name=args.name+args.version)
    data_module = SamshadowDataModule(args)
    data_module.setup("test")
    model = diffusion.load_from_checkpoint(f'/home/xinrui/projects/ShadowDiffusion/experiments/train_diffusion/last.ckpt')
    tester = L.Trainer(
        accelerator='gpu',
        devices=args.test.gpu_ids,
        max_epochs=-1,
        benchmark=True,
        logger=logger
    )
    test_result = tester.test(model, data_module)
    PSNR_SSIM_list_with_name = test_result[0]
    with open(f'./experiments_lightning/{args.name}/PSNR_SSIM_list.log', 'w') as file:
        for key, value in PSNR_SSIM_list_with_name.items():
            file.write(f"{key}: {value}\n")

@hydra.main(config_path="./config", config_name="sam_shadow_SRD", version_base=None)
def main(args: DictConfig) -> None:
    torch.set_float32_matmul_precision("high")
    L.seed_everything(1234)

    if args.phase == 'train':
        train(args)
    elif args.phase == 'test':
        sample(args)


if __name__ == "__main__":
    # config = OmegaConf.load("config/sam_shadow_ISTD.yaml")
    # args = AttrDict(config)
    # main(args)
    main()


