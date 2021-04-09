"""
@Time    : 2021/2/6 15:24
@Author  : Xiao Qinfeng
@Email   : qfxiao@bjtu.edu.cn
@File    : model.py
@Software: PyCharm
@Desc    : 
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

from .backbone import Encoder, Encoder3d, ResNet2d3d


@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor)
                      for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output


class DCC(nn.Module):
    def __init__(self, input_size, input_channels, feature_dim, use_temperature, temperature, device, strides=None,
                 mode='raw'):
        super(DCC, self).__init__()

        self.input_size = input_size
        self.input_channels = input_channels
        self.feature_dim = feature_dim
        self.use_temperature = use_temperature
        self.temperature = temperature
        self.device = device
        self.mode = mode

        if mode == 'raw':
            self.encoder = Encoder(input_size, input_channels, feature_dim)
        elif mode == 'sst':
            # self.encoder = ResNet2d3d(input_size=input_size, input_channel=input_channels, feature_dim=feature_dim)
            self.encoder = Encoder3d(input_size=input_size, input_channel=input_channels, feature_dim=feature_dim)
        else:
            raise ValueError

        self.targets = None

    @torch.no_grad()
    def _batch_shuffle_ddp(self, x):
        '''
        Batch shuffle, for making use of BatchNorm.
        *** Only support DistributedDataParallel (DDP) model. ***
        '''
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # random shuffle index
        idx_shuffle = torch.randperm(batch_size_all).cuda()

        # broadcast to all gpus
        torch.distributed.broadcast(idx_shuffle, src=0)

        # index for restoring
        idx_unshuffle = torch.argsort(idx_shuffle)

        # shuffled index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this], idx_unshuffle

    @torch.no_grad()
    def _batch_unshuffle_ddp(self, x, idx_unshuffle):
        '''
        Undo batch shuffle.
        *** Only support DistributedDataParallel (DDP) model. ***
        '''
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # restored index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_unshuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this]

    def forward(self, x):
        # Extract feautres
        # x: (batch, num_seq, channel, seq_len)
        if self.mode == 'raw':
            batch_size, num_epoch, channel, time_len = x.shape
            x = x.view(batch_size * num_epoch, *x.shape[2:])
        else:
            batch_size, num_epoch, time_len, width, height = x.shape
            x = x.view(batch_size * num_epoch, 1, *x.shape[2:])
        feature = self.encoder(x)
        feature = F.normalize(feature, p=2, dim=1)
        feature = feature.view(batch_size, num_epoch, self.feature_dim)

        #################################################################
        #                       Multi-InfoNCE Loss                      #
        #################################################################
        mask = torch.zeros(batch_size, num_epoch, num_epoch, batch_size, dtype=bool)
        for i in range(batch_size):
            for j in range(num_epoch):
                mask[i, j, :, i] = 1
        mask = mask.cuda(self.device)

        logits = torch.einsum('ijk,mnk->ijnm', [feature, feature])
        # if self.use_temperature:
        #     logits /= self.temperature

        pos = torch.exp(logits.masked_select(mask).view(batch_size, num_epoch, num_epoch)).sum(-1)
        neg = torch.exp(logits.masked_select(torch.logical_not(mask)).view(batch_size, num_epoch,
                                                                           batch_size * num_epoch - num_epoch)).sum(-1)

        loss = (-torch.log(pos / (pos + neg))).mean()

        return loss

        # Compute scores
        # logits = torch.einsum('ijk,kmn->ijmn', [pred, feature])  # (batch, pred_step, num_seq, batch)
        # logits = logits.view(batch_size * self.pred_steps, num_epoch * batch_size)

        # logits = torch.einsum('ijk,mnk->ijnm', [feature, feature])
        # # print('3. Logits: ', logits.shape)
        # logits = logits.view(batch_size * num_epoch, num_epoch * batch_size)
        # if self.use_temperature:
        #     logits /= self.temperature
        #
        # if self.targets is None:
        #     targets = torch.zeros(batch_size, num_epoch, num_epoch, batch_size)
        #     for i in range(batch_size):
        #         for j in range(num_epoch):
        #             targets[i, j, :, i] = 1
        #     targets = targets.view(batch_size * num_epoch, num_epoch * batch_size)
        #     targets = targets.argmax(dim=1)
        #     targets = targets.cuda(device=self.device)
        #     self.targets = targets
        #
        # return logits, self.targets

    def _initialize_weights(self, module):
        for name, param in module.named_parameters():
            if 'bias' in name:
                nn.init.constant_(param, 0.0)
            elif 'weight' in name:
                nn.init.orthogonal_(param, 1)


class DCCClassifier(nn.Module):
    def __init__(self, input_size, input_channels, feature_dim, num_class, use_l2_norm, use_dropout, use_batch_norm,
                 device, strides=None, mode='raw'):
        super(DCCClassifier, self).__init__()

        self.input_size = input_size
        self.input_channels = input_channels
        self.feature_dim = feature_dim
        self.device = device
        self.use_l2_norm = use_l2_norm
        self.use_dropout = use_dropout
        self.use_batch_norm = use_batch_norm
        self.mode = mode

        if mode == 'raw':
            self.encoder = Encoder(input_size, input_channels, feature_dim)
        elif mode == 'sst':
            self.encoder = Encoder3d(input_size=input_size, input_channel=input_channels, feature_dim=feature_dim)
        else:
            raise ValueError

        final_fc = []

        if use_batch_norm:
            final_fc.append(nn.BatchNorm1d(feature_dim))
        if use_dropout:
            final_fc.append(nn.Dropout(0.5))
        final_fc.append(nn.Linear(feature_dim, num_class))
        self.final_fc = nn.Sequential(*final_fc)

        # self._initialize_weights(self.final_fc)

    def forward(self, x):
        if self.mode == 'raw':
            batch_size, num_epoch, channel, time_len = x.shape
            x = x.view(batch_size * num_epoch, *x.shape[2:])
        else:
            batch_size, num_epoch, time_len, width, height = x.shape
            x = x.view(batch_size * num_epoch, 1, *x.shape[2:])
        feature = self.encoder(x)
        # feature = feature.view(batch_size, num_epoch, self.feature_dim)

        if self.use_l2_norm:
            feature = F.normalize(feature, p=2, dim=1)

        out = self.final_fc(feature)
        out = out.view(batch_size, num_epoch, -1)

        # print('3. Out: ', out.shape)

        return out


class MME(nn.Module):
    def __init__(self, input_size_v1, input_size_v2, input_channels, feature_dim, use_temperature, temperature, device,
                 strides=None,
                 mode='raw'):
        super(MME, self).__init__()

        self.input_size_v1 = input_size_v1
        self.input_size_v2 = input_size_v2
        self.input_channels = input_channels
        self.feature_dim = feature_dim
        self.use_temperature = use_temperature
        self.temperature = temperature
        self.device = device
        self.mode = mode

        if mode == 'raw':
            self.encoder = Encoder(input_size_v1, input_channels, feature_dim)
            self.sampler = Encoder(input_size_v2, input_channels, feature_dim)
        elif mode == 'sst':
            # self.encoder = ResNet2d3d(input_size=input_size, input_channel=input_channels, feature_dim=feature_dim)
            self.encoder = Encoder3d(input_size=input_size_v1, input_channel=input_channels, feature_dim=feature_dim)
            self.sampler = Encoder3d(input_size=input_size_v2, input_channel=input_channels, feature_dim=feature_dim)
        else:
            raise ValueError

        for param_s in self.sampler.parameters():
            param_s.requires_grad = False  # not update by gradient

    @torch.no_grad()
    def _batch_shuffle_ddp(self, x):
        '''
        Batch shuffle, for making use of BatchNorm.
        *** Only support DistributedDataParallel (DDP) model. ***
        '''
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # random shuffle index
        idx_shuffle = torch.randperm(batch_size_all).cuda()

        # broadcast to all gpus
        torch.distributed.broadcast(idx_shuffle, src=0)

        # index for restoring
        idx_unshuffle = torch.argsort(idx_shuffle)

        # shuffled index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this], idx_unshuffle

    @torch.no_grad()
    def _batch_unshuffle_ddp(self, x, idx_unshuffle):
        '''
        Undo batch shuffle.
        *** Only support DistributedDataParallel (DDP) model. ***
        '''
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # restored index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_unshuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this]

    def forward(self, x1, x2):
        # Extract feautres
        # x: (batch, num_seq, channel, seq_len)
        batch_size, num_epoch, time_len, width, height = x1.shape
        x1 = x1.view(batch_size * num_epoch, 1, *x1.shape[2:])
        feature_k = self.encoder(x1)
        feature_k = F.normalize(feature_k, p=2, dim=1)
        feature_k = feature_k.view(batch_size, num_epoch, self.feature_dim)

        with torch.no_grad():
            x2 = x2.view(batch_size * num_epoch, 1, *x2.shape[2:])
            feature_q = self.sampler(x2)
            feature_q = F.normalize(feature_q, p=2, dim=1)
            feature_q = feature_q.view(batch_size, num_epoch, self.feature_dim)

        #################################################################
        #                       Multi-InfoNCE Loss                      #
        #################################################################
        mask = torch.zeros(batch_size, num_epoch, num_epoch, batch_size, dtype=bool)
        for i in range(batch_size):
            for j in range(num_epoch):
                mask[i, j, :, i] = 1
        mask = mask.cuda(self.device)

        logits = torch.einsum('ijk,mnk->ijnm', [feature_k, feature_k])
        # if self.use_temperature:
        #     logits /= self.temperature

        sim = torch.einsum('ijk,mnk->ijnm', [feature_q, feature_q])

        pos = torch.exp(logits.masked_select(mask).view(batch_size, num_epoch, num_epoch)).sum(-1)
        neg = torch.exp(logits.masked_select(torch.logical_not(mask)).view(batch_size, num_epoch,
                                                                           batch_size * num_epoch - num_epoch))
        neg_v2 = sim.masked_select(torch.logical_not(mask)).view(batch_size, num_epoch,
                                                                 batch_size * num_epoch - num_epoch)

        neg = (neg * neg_v2).sum(-1)

        loss = (-torch.log(pos / (pos + neg))).mean()

        return loss

    def _initialize_weights(self, module):
        for name, param in module.named_parameters():
            if 'bias' in name:
                nn.init.constant_(param, 0.0)
            elif 'weight' in name:
                nn.init.orthogonal_(param, 1)


if __name__ == '__main__':
    model = DCC(input_size=200, input_channels=62, feature_dim=128, use_temperature=True, temperature=0.07,
                device=0, strides=None, mode='raw')
    model = model.cuda()
    out = model(torch.randn(16, 10, 62, 200).cuda())
    print(out)
