#!/usr/bin/env python

import argparse
import os
import os.path as osp
import torch.nn.functional as F

# import matplotlib
# matplotlib.use('TkAgg')
# import matplotlib.pyplot as plt

import torch
# torch.backends.cudnn.enabled=False
# torch.backends.cudnn.benchmark = True
from torch.autograd import Variable
import tqdm
from dataloaders import fundus_dataloader as DL
from torch.utils.data import DataLoader
from dataloaders import custom_transforms as tr
from torchvision import transforms
# from scipy.misc import imsave
from matplotlib.pyplot import imsave
from utils.Utils import *
from utils.metrics import *
from datetime import datetime
import pytz
import networks.deeplabv3 as netd
import networks.deeplabv3_eval as netd_eval
import cv2
import torch.backends.cudnn as cudnn
import random
from tensorboardX import SummaryWriter



bceloss = torch.nn.BCELoss(reduction='none')
seed = 3377
savefig = False
get_hd = True
model_save = True
if True:
    cudnn.benchmark = False
    cudnn.deterministic = True
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-file', type=str, default='/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/base.pth.tar')
    parser.add_argument('--dataset', type=str, default='Domain2')
    parser.add_argument('--source', type=str, default='Domain3')
    parser.add_argument('-g', '--gpu', type=int, default=0)
    parser.add_argument('--data-dir', default='../datasets/Fundus')
    parser.add_argument('--out-stride',type=int,default=16)
    parser.add_argument('--sync-bn',type=bool,default=True)
    parser.add_argument('--freeze-bn',type=bool,default=False)
    args = parser.parse_args()

    # os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)
    model_file = args.model_file

    # 1. dataset
    composed_transforms_train = transforms.Compose([
        tr.Resize(512),
        tr.add_salt_pepper_noise(),
        tr.adjust_light(),
        tr.eraser(),
        tr.Normalize_tf(),
        tr.ToTensor()
    ])
    composed_transforms_test = transforms.Compose([
        tr.Resize(512),
        tr.Normalize_tf(),
        tr.ToTensor()
    ])
    db_train = DL.FundusSegmentation(base_dir=args.data_dir, dataset=args.dataset, split='train/ROIs', transform=composed_transforms_train)
    db_test = DL.FundusSegmentation(base_dir=args.data_dir, dataset=args.dataset, split='test/ROIs', transform=composed_transforms_test)
    db_source = DL.FundusSegmentation(base_dir=args.data_dir, dataset=args.source, split='train/ROIs', transform=composed_transforms_test)

    train_loader = DataLoader(db_train, batch_size=8, shuffle=False, num_workers=1)
    test_loader = DataLoader(db_test, batch_size=1, shuffle=False, num_workers=1)
    source_loader = DataLoader(db_source, batch_size=1, shuffle=False, num_workers=1)

    # 2. model
    model = netd.DeepLab(num_classes=2, backbone='mobilenet', output_stride=args.out_stride, sync_bn=args.sync_bn, freeze_bn=args.freeze_bn)
    model_eval = netd_eval.DeepLab(num_classes=2, backbone='mobilenet', output_stride=args.out_stride, sync_bn=args.sync_bn, freeze_bn=args.freeze_bn).cuda()

    if torch.cuda.is_available():
        model = model.cuda()
    print('==> Loading %s model file: %s' %
          (model.__class__.__name__, model_file))
    checkpoint = torch.load(model_file)

    model.load_state_dict(checkpoint['model_state_dict'])

    model.train()

    if args.dataset=="Domain2":
        npfilename = '/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/results/prototype/pseudolabel_D2_original_2408.npz'
        # npfilename = '/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/results/prototype/pseudolabel_D2_bar.npz'
        # npfilename = '/users/scratch/baryaacovi-2024-06-01/projects/SFDA-DPL/results/prototype/pseudolabel_D2_bar_cup_on_disc_gt_fg.npz'

    elif args.dataset=="Domain1":
        npfilename = './generate_pseudo/pseudolabel_D1.npz'

    npdata = np.load(npfilename, allow_pickle=True)
    pseudo_label_dic = npdata['arr_0'].item()
    uncertain_dic = npdata['arr_1'].item()
    proto_pseudo_dic = npdata['arr_2'].item()

    var_list = model.named_parameters()

    optim_gen = torch.optim.Adam(model.parameters(), lr=0.002, betas=(0.9, 0.99))
    best_val_cup_dice = 0.0;
    best_val_disc_dice = 0.0;
    best_avg = 0.0

    iter_num = 0
    
    out = './logs_bar'
    log_dir = os.path.join(out, 'tensorboard',
                               datetime.now().strftime('%b%d_%H-%M-%S') + '_')
    writer = SummaryWriter(log_dir=log_dir)
    
    plabs = list(pseudo_label_dic.values())
    tp = torch.from_numpy(np.asarray(plabs)).float().cuda()
    num_cup = tp[:,0,...].sum()
    num_cup_bg = (1 - tp)[:,0,...].sum()
    num_disc = tp[:,1,...].sum()
    num_disc_bg = (1 - tp)[:,1,...].sum()
    
    cup_bg_coeff = num_cup / num_cup_bg
    disc_bg_coeff = num_disc / num_disc_bg
    print("coeffs : disc_bg_coeff: ", disc_bg_coeff, " cup_bg_coeff: ", cup_bg_coeff, "tp shape: ", tp.shape)
    
    for epoch_num in tqdm.tqdm(range(2), ncols=70):
        model.train()
        epoch_cup_avg_loss = 0.0
        epoch_disc_avg_loss = 0.0
        num_img_tr = len(train_loader)
        for batch_idx, (sample) in enumerate(train_loader):
            data, target, img_name = sample['image'], sample['map'], sample['img_name']
            if torch.cuda.is_available():
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data), Variable(target)

            prediction, _, feature = model(data)
            prediction = torch.sigmoid(prediction)

            pseudo_label = [pseudo_label_dic.get(key) for key in img_name]
            uncertain_map = [uncertain_dic.get(key) for key in img_name]
            # 1 if th e distance to the fg class prototype is smaller than the distance to the bg class prototype
            # Same for the bg class case
            proto_pseudo = [proto_pseudo_dic.get(key) for key in img_name]

            pseudo_label = torch.from_numpy(np.asarray(pseudo_label)).float().cuda()
            uncertain_map = torch.from_numpy(np.asarray(uncertain_map)).float().cuda()
            proto_pseudo = torch.from_numpy(np.asarray(proto_pseudo)).float().cuda()

            for param in model.parameters():
                param.requires_grad = True
            # TRY FREEZING
            # for name, p in model.named_parameters():
            #     if 'decoder.last_conv.3.weight' in name:
            #         prototypes = p
            #         p.requires_grad = False
            #         print(f"freeze {name}")
            #     if 'decoder.last_conv.3.bias' in name:
            #         bias = p
            #         p.requires_grad = False
            #         print(f"freeze {name}")
            optim_gen.zero_grad()

            target_0_obj = F.interpolate(pseudo_label[:,0:1,...], size=feature.size()[2:], mode='nearest')
            target_1_obj = F.interpolate(pseudo_label[:, 1:, ...], size=feature.size()[2:], mode='nearest')
            target_0_bck = 1.0 - target_0_obj;target_1_bck = 1.0 - target_1_obj

            # pixel level denoising
            mask_0_obj = torch.zeros([pseudo_label.shape[0], 1, pseudo_label.shape[2], pseudo_label.shape[3]]).cuda()
            mask_0_bck = torch.zeros([pseudo_label.shape[0], 1, pseudo_label.shape[2], pseudo_label.shape[3]]).cuda()
            mask_1_obj = torch.zeros([pseudo_label.shape[0], 1, pseudo_label.shape[2], pseudo_label.shape[3]]).cuda()
            mask_1_bck = torch.zeros([pseudo_label.shape[0], 1, pseudo_label.shape[2], pseudo_label.shape[3]]).cuda()
            mask_0_obj[uncertain_map[:, 0:1, ...] < 0.05] = 1.0
            mask_0_bck[uncertain_map[:, 0:1, ...] < 0.05] = 1.0
            mask_1_obj[uncertain_map[:, 1:, ...] < 0.05] = 1.0
            mask_1_bck[uncertain_map[:, 1:, ...] < 0.05] = 1.0
            
            # the pseudo labels after  the pixel level denoising
            # we take the pseudo labels (1 or 0) 
            mask = torch.cat((mask_0_obj*pseudo_label[:,0:1,...] + mask_0_bck*(1.0-pseudo_label[:,0:1,...]), mask_1_obj*pseudo_label[:,1:,...] + mask_1_bck*(1.0-pseudo_label[:,1:,...])), dim=1)

            mask_proto = torch.zeros([data.shape[0], 2, data.shape[2], data.shape[3]]).cuda()
            # I[y==1] * I[d_obj < d_bck]
            mask_proto[pseudo_label==proto_pseudo] = 1.0

            # combine with pixel denoising
            mask = mask*mask_proto
            
            # keep only the pixels that are in the disc for the cup loss
            
            only_disc = mask[:,0,...]  * pseudo_label[:,1,...]
            only_disc_ratio = torch.sum(only_disc) / torch.sum(mask[:,0,...])
            # print("only_disc_ratio: ", only_disc_ratio)
            
            # num_pix_a = torch.sum(mask[:,0,...])
            # print("mask[:,0,...]: BEFORE removal of non-disc", torch.sum(mask[:,0,...]))
            mask_copy = mask.clone()
            mask_copy[:,0,...] =  (mask_copy[:,0,...]  * pseudo_label[:,1,...])
            # num_pix_b = torch.sum(mask[:,0,...])
            # print("mask[:,0,...]: AFTER removal of non-disc", torch.sum(mask[:,0,...]))
            # print(f"Ratio reduction: {num_pix_a/num_pix_b}")
            # cup: 0.7880 disc: 0.9017 avg: 0.8448 cup: 10.3987 disc: 9.8523 avg: 10.1255
            #  best cup: 0.7958 best disc: 0.9023 best avg: 0.8490 best cup: 9.1075 best disc: 9.3767 best avg: 9.2421
            
            loss_seg_pixel = bceloss(prediction, pseudo_label)
            
            # weight the loss
            # mask[:,0,...][pseudo_label[:,0,...] == 0] *= cup_bg_coeff 
            # mask[:,1,...][pseudo_label[:,1,...] == 0] *= disc_bg_coeff
            
            loss_seg_cup = torch.sum(mask[:,0,...] * loss_seg_pixel[:,0,...])  / torch.sum(mask[:,0,...])
            loss_seg_disc = torch.sum(mask[:,1,...] * loss_seg_pixel[:,1,...]) / torch.sum(mask[:,1,...])
            
            # loss_seg_cup_copy = torch.sum(mask_copy[:,0,...] * loss_seg_pixel[:,0,...])  / torch.sum(mask_copy[:,0,...])
            
            # print(f"Seg copy - cup : {loss_seg_cup_copy - loss_seg_cup}")            
            
            epoch_cup_avg_loss += loss_seg_cup
            epoch_disc_avg_loss += loss_seg_disc
            
            # loss_seg_disc = 0
            # print(f"CUP LOSS {loss_seg_cup}")
            # print(f"DISC LOSS {loss_seg_disc}")
            
            (loss_seg_cup + loss_seg_disc).backward()
            optim_gen.step()
            iter_num = iter_num + 1
        print(f"\nEpoch {epoch_num} Ratio:{(epoch_disc_avg_loss/num_img_tr)/(epoch_cup_avg_loss/num_img_tr)} cup loss: {epoch_cup_avg_loss/num_img_tr} disc loss: {epoch_disc_avg_loss/num_img_tr}")

        #test
        model_eval.train()
        pretrained_dict = model.state_dict()
        model_dict = model_eval.state_dict()
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        model_eval.load_state_dict(pretrained_dict)

        val_cup_dice = 0.0;val_disc_dice = 0.0;datanum_cnt = 0.0
        cup_hd = 0.0; disc_hd = 0.0;datanum_cnt_cup = 0.0;datanum_cnt_disc = 0.0
        with torch.no_grad():
            for batch_idx, (sample) in enumerate(test_loader):
                data, target, img_name = sample['image'], sample['map'], sample['img_name']
                if torch.cuda.is_available():
                    data, target = data.cuda(), target.cuda()
                data, target = Variable(data), Variable(target)
                prediction, boundary, _ = model_eval(data)
                prediction = torch.sigmoid(prediction)

                target_numpy = target.data.cpu()
                prediction = prediction.data.cpu()
                prediction[prediction>0.75] = 1;prediction[prediction <= 0.75] = 0


                cup_dice = dice_coefficient_numpy(prediction[:,0, ...], target_numpy[:, 0, ...])
                disc_dice = dice_coefficient_numpy(prediction[:,1, ...], target_numpy[:, 1, ...])

                for i in range(prediction.shape[0]):
                    hd_tmp = hd_numpy(prediction[i, 0, ...], target_numpy[i, 0, ...], get_hd)
                    if np.isnan(hd_tmp):
                        datanum_cnt_cup -= 1.0
                    else:
                        cup_hd += hd_tmp

                    hd_tmp = hd_numpy(prediction[i, 1, ...], target_numpy[i, 1, ...], get_hd)
                    if np.isnan(hd_tmp):
                        datanum_cnt_disc -= 1.0
                    else:
                        disc_hd += hd_tmp

                val_cup_dice += np.sum(cup_dice)
                val_disc_dice += np.sum(disc_dice)

                datanum_cnt += float(prediction.shape[0])
                datanum_cnt_cup += float(prediction.shape[0])
                datanum_cnt_disc += float(prediction.shape[0])

        val_cup_dice /= datanum_cnt
        val_disc_dice /= datanum_cnt
        cup_hd /= datanum_cnt_cup
        disc_hd /= datanum_cnt_disc
        if (val_cup_dice+val_disc_dice)/2.0>best_avg:
            best_val_cup_dice = val_cup_dice; best_val_disc_dice = val_disc_dice; best_avg = (val_cup_dice+val_disc_dice)/2.0
            best_cup_hd = cup_hd; best_disc_hd = disc_hd; best_avg_hd = (best_cup_hd+best_disc_hd)/2.0

        if not os.path.exists('./logs/train_target'):
            os.mkdir('./logs/train_target')
        if args.dataset == 'Domain1':
            savefile = './logs/train_target/' + 'D1_' + 'checkpoint_%d.pth.tar' % epoch_num
        elif args.dataset == 'Domain2':
            savefile = './logs/train_target/' + 'D2_' + 'checkpoint_%d.pth.tar' % epoch_num
        if model_save:
            torch.save({
                'model_state_dict': model.state_dict(),
                'best_mean_dice': best_avg,
                'best_cup_dice': best_val_cup_dice,
                'best_disc_dice': best_val_disc_dice,
            }, savefile)

        print("cup: %.4f disc: %.4f avg: %.4f cup: %.4f disc: %.4f avg: %.4f" %
              (val_cup_dice, val_disc_dice, (val_cup_dice+val_disc_dice)/2.0, cup_hd, disc_hd, (cup_hd+disc_hd)/2.0))
        print("best cup: %.4f best disc: %.4f best avg: %.4f best cup: %.4f best disc: %.4f best avg: %.4f" %
              (best_val_cup_dice, best_val_disc_dice, best_avg, best_cup_hd, best_disc_hd, best_avg_hd))
        model.train()




