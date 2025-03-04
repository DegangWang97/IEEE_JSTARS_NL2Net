import torch
import torch.nn as nn
import torch.nn.functional as F
from utils import SSBlock, SSBlockNaive

class NL2Net(nn.Module):
    def __init__(self, factor=2, nch_in=189, nch_out=189, nch_ker=64, nblk=6, mode='ss', f_scale=2, ss_exp_factor=1.):
        super().__init__()
        
        self.factor = factor
        ly = []
        ly += [ nn.Conv2d(nch_in, nch_ker, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        self.head = nn.Sequential(*ly)

        self.branch1 = DC_branch1(3, nch_ker, nblk)
        self.branch2 = DC_branch2(3, nch_ker, nblk, mode, f_scale=f_scale, ss_exp_factor=1.)

        ly = []
        ly += [ nn.Conv2d(nch_ker*2,  nch_ker,    kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(nch_ker,    nch_ker//2, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(nch_ker//2, nch_out,    kernel_size=1) ]
        self.tail = nn.Sequential(*ly)

    def forward(self, x):
        if self.factor > 1:
            x_pd = pixel_shuffle_down_sampling(x, self.factor, pad=0)
        else:
            x_pd = x
        
        x_head = self.head(x_pd)

        br1 = self.branch1(x_head)
        br2 = self.branch2(x_head)

        x_out_pd = self.tail(torch.cat([br1, br2], dim=1))

        if self.factor > 1:
            x_bkg = pixel_shuffle_up_sampling(x_out_pd, self.factor, pad=0)
        else:
            x_bkg = x_out_pd
        
        return x_bkg

    def _initialize_weights(self):
        # Liyong version
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                # n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, (2 / (9.0 * 64)) ** 0.5)

class DC_branch1(nn.Module):
    def __init__(self, stride, nch_in, nblk):
        super().__init__()

        ly = []
        ly += [ BlockMaskedConv2d(nch_in, nch_in, kernel_size=2*stride-1, stride=1, padding=stride-1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(nch_in, nch_in, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(nch_in, nch_in, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]

        ly += [ DCl(stride, nch_in) for _ in range(nblk) ]

        ly += [ nn.Conv2d(nch_in, nch_in, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        
        self.body = nn.Sequential(*ly)

    def forward(self, x):
        return self.body(x)

class DCl(nn.Module):
    def __init__(self, stride, nch_in):
        super().__init__()

        ly = []
        ly += [ nn.Conv2d(nch_in, nch_in, kernel_size=3, stride=1, padding=stride, dilation=stride) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(nch_in, nch_in, kernel_size=1) ]
        self.body = nn.Sequential(*ly)

    def forward(self, x):
        return x + self.body(x)

class BlockMaskedConv2d(nn.Conv2d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.register_buffer('mask', self.weight.data.clone())
        _, _, kH, kW = self.weight.size()
        self.mask.fill_(1)
        self.mask[:, :, 1:-1, 1:-1] = 0

    def forward(self, x):
        self.weight.data *= self.mask
        return super().forward(x)


class DC_branch2(nn.Module):
    def __init__(self, stride, in_ch, num_module, mode='ss', f_scale=2, ss_exp_factor=1.):
        super().__init__()

        ly = []
        ly += [ BlockMaskedConv2d(in_ch, in_ch, kernel_size=2*stride-1, stride=1, padding=stride-1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(in_ch, in_ch, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        ly += [ nn.Conv2d(in_ch, in_ch, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]

        if isinstance(mode, str):
            mode = [mode for _ in range(num_module)]
        elif isinstance(mode, list):
            assert len(mode) == num_module, f"{len(mode)} != number of bsn modules"
        else:
            assert False, 'Invalid mode type'
        
        for bmode in mode:
            if bmode == 'na':
                ly += [SSBlockNaive(stride, in_ch)]
            elif bmode == 'ss':
                ly += [SSBlock(stride, in_ch, f_scale=f_scale, ss_exp_factor=1.)]
            else:
                assert False, f"Invalid mode: {bmode}"

        ly += [ nn.Conv2d(in_ch, in_ch, kernel_size=1) ]
        ly += [ nn.ReLU(inplace=True) ]
        
        self.body = nn.Sequential(*ly)

    def forward(self, x):
        return self.body(x)


def pixel_shuffle_down_sampling(x:torch.Tensor, f:int, pad:int=0, pad_value:float=0.):
    '''
    pixel-shuffle down-sampling (PD) from "When AWGN-denoiser meets real-world noise." (AAAI 2019)
    Args:
        x (Tensor) : input tensor
        f (int) : factor of PD
        pad (int) : number of pad between each down-sampled images
        pad_value (float) : padding value
    Return:
        pd_x (Tensor) : down-shuffled image tensor with pad or not
    '''
    # single image tensor
    if len(x.shape) == 3:
        c,w,h = x.shape
        unshuffled = F.pixel_unshuffle(x, f)
        if pad != 0: unshuffled = F.pad(unshuffled, (pad, pad, pad, pad), value=pad_value)
        return unshuffled.view(c,f,f,w//f+2*pad,h//f+2*pad).permute(0,1,3,2,4).reshape(c, w+2*f*pad, h+2*f*pad)
    # batched image tensor
    else:
        b,c,w,h = x.shape
        unshuffled = F.pixel_unshuffle(x, f)
        if pad != 0: unshuffled = F.pad(unshuffled, (pad, pad, pad, pad), value=pad_value)
        return unshuffled.view(b,c,f,f,w//f+2*pad,h//f+2*pad).permute(0,1,2,4,3,5).reshape(b,c,w+2*f*pad, h+2*f*pad)

def pixel_shuffle_up_sampling(x:torch.Tensor, f:int, pad:int=0):
    '''
    inverse of pixel-shuffle down-sampling (PD)
    see more details about PD in pixel_shuffle_down_sampling()
    Args:
        x (Tensor) : input tensor
        f (int) : factor of PD
        pad (int) : number of pad will be removed
    '''
    # single image tensor
    if len(x.shape) == 3:
        c,w,h = x.shape
        before_shuffle = x.view(c,f,w//f,f,h//f).permute(0,1,3,2,4).reshape(c*f*f,w//f,h//f)
        if pad != 0: before_shuffle = before_shuffle[..., pad:-pad, pad:-pad]
        return F.pixel_shuffle(before_shuffle, f)
    # batched image tensor
    else:
        b,c,w,h = x.shape
        before_shuffle = x.view(b,c,f,w//f,f,h//f).permute(0,1,2,4,3,5).reshape(b,c*f*f,w//f,h//f)
        if pad != 0: before_shuffle = before_shuffle[..., pad:-pad, pad:-pad]
        return F.pixel_shuffle(before_shuffle, f)