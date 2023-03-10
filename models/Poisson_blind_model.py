import torch
from .base_model import BaseModel
from . import networks
import torch.nn as nn
#from skimage.measure import peak_signal_noise_ratio
from skimage.metrics import peak_signal_noise_ratio
import warnings
warnings.filterwarnings('ignore')
from util.util import calc_psnr
import numpy as np
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn import L1Loss, MSELoss
from torch.autograd import Variable

class L1_TVLoss_Charbonnier(nn.Module):
    def __init__(self):
        super(L1_TVLoss_Charbonnier, self).__init__()
        self.e = 0.000001 ** 2
    def forward(self, x):
        batch_size = x.size()[0]
        h_tv = torch.abs((x[:, :, 1:, :]-x[:, :, :-1, :]))
        h_tv = torch.mean(torch.sqrt(h_tv ** 2 + self.e))
        w_tv = torch.abs((x[:, :, :, 1:]-x[:, :, :, :-1]))
        w_tv = torch.mean(torch.sqrt(w_tv ** 2 + self.e))
        return h_tv + w_tv

class PoissonblindModel(BaseModel):

    def __init__(self, opt):
        BaseModel.__init__(self, opt)
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['MSE','f','sigma','eta_s']
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        self.visual_names = ['lr_in','hr','recon']
        if self.isTrain:
            self.model_names = ['f']
        else:  # during test time, only load G
            self.model_names = ['f']
            self.visual_names = ['recon']
        # define networks (both generator and discriminator)
        self.netf = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, 'unet', opt.norm,
                                      not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids)
        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionL1 = torch.nn.L1Loss()
            self.criterionL2 = torch.nn.MSELoss()
            self.criterionKL = torch.nn.KLDivLoss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            self.optimizer_f = torch.optim.Adam(self.netf.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizers.append(self.optimizer_f)
                        
        self.TVL1loss = L1_TVLoss_Charbonnier()
        self.batch = opt.batch_size
        self.eta = opt.scale_param
        self.sigma_min = 0.5
        self.sigma_max = 1
        self.sigma_annealing = 2*11000       
    
    def set_input(self, input):
        AtoB = self.opt.direction == 'AtoB'
        self.hr = input['B' if AtoB else 'A'].to(self.device,dtype = torch.float32)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']  
        self.lr_in = np.random.poisson(self.hr.cpu().numpy()/self.eta_s)*self.eta_s 
        self.lr_in = torch.from_numpy(self.lr_in).to(self.device,dtype = torch.float32)
        self.lr = self.lr_in/self.eta_s
        
    def set_input_val(self, input):
        AtoB = self.opt.direction == 'AtoB'
        self.lr_in = input['A' if AtoB else 'B'].to(self.device,dtype = torch.float32)
        self.lr = self.lr_in/self.eta
        self.hr = input['B' if AtoB else 'A'].to(self.device,dtype = torch.float32)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']   
        self.k = self.lr.shape[2]*self.lr.shape[3]*self.lr.shape[0]
        
    def set_input_test(self, input):
        AtoB = self.opt.direction == 'AtoB'
        self.lr_in = input['A' if AtoB else 'B'].to(self.device,dtype = torch.float32)        
        self.hr = input['B' if AtoB else 'A'].to(self.device,dtype = torch.float32)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']        
        
    def set_param_s(self, iter):       
        self.min_log = np.log([0.005])
        self.eta_now = 0.1
        self.eta_s = self.min_log + np.random.rand(1) * (np.log([self.eta_now]) - self.min_log)
        self.eta_s = np.exp(self.eta_s)                
        self.eta_s = self.eta_s[0]
        self.loss_eta_s = self.eta_s
        
    def set_sigma(self, iter):
        perc = min((iter+1)/float(self.sigma_annealing), 1.0)
        self.sigma = self.sigma_max * (1-perc) + self.sigma_min * perc
        self.loss_sigma = self.sigma
        
    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        self.recon = (self.eta_s*0.5 + self.lr*self.eta_s)*torch.exp(self.netf(self.lr,0)[0])
    def forward_test(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        self.recon = (self.eta*0.5 + self.lr*self.eta)*torch.exp(self.netf(self.lr,0)[0])
        
    def forward_tv(self,i):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        eta_max = 0.005
        eta_min = 0.1
        best_loss = 2
        best_eta = 0
        tvlosses = []
        
        for iter in range(100):
            perc = min((iter+1)/float(100), 1)
            eta = eta_max* (perc) + eta_min *(1-perc)
            ones = torch.ones(self.lr_in.shape).to(self.device,dtype = torch.float32)
            self.lr = self.lr_in/eta
            recon = (eta*0.5 + self.lr*eta)*torch.exp(self.netf(self.lr,0)[0])
            idx=[recon>0]
            recons = torch.clamp(recon,0,recon.max())
            tvloss = 0.1*self.TVL1loss(recons) + torch.mean(recon[idx] - self.lr_in[idx]*torch.log(recon[idx]))
            tvlosses.append(tvloss)
            if tvloss < best_loss:
                best_loss = tvloss
                best_eta = eta
                self.recon = recon
            print('Current eta %f TVL1loss: %.4f Best_TVloss(Best eta %f) : %.4f' % (eta,tvloss,best_eta,best_loss))
        print("------------------{}th processing Done----------------".format(i+1))
        
    def forward_psnr(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        self.recon = (self.eta*0.5 + self.lr*self.eta)*torch.exp(self.netf(self.lr,0)[0])
        self.recon = torch.clamp(self.recon.detach().cpu(), 0, 1)
        self.hr = self.hr.detach().cpu()
        psnr = calc_psnr(self.recon,self.hr)             
        _,loss = self.netf(self.lr,self.sigma_min)        
        return  psnr,loss 
    
    def backward_f(self):
        """Calculate GAN and L1 loss for the generator"""            
        self.output,self.loss_f = self.netf(self.lr,self.sigma)     
        self.loss_MSE = self.criterionL2(self.recon,self.hr)
        self.loss_f.backward()
        
    def optimize_parameters(self):
        self.forward()                  # compute fake images: G(A)     
        self.optimizer_f.zero_grad()        # set G's gradients to zero
        self.backward_f()                   # calculate graidents for G
        self.optimizer_f.step()              # udpate G's weights              