import time
import os
import math
import argparse
from glob import glob
from collections import OrderedDict
import random
import warnings
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
import pandas as pd
import joblib
import cv2
import yaml

from sklearn.model_selection import KFold, StratifiedKFold, train_test_split
from skimage.io import imread

from apex import amp

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler
import torch.backends.cudnn as cudnn
import torchvision

from lib.datasets import Dataset
from lib.utils.utils import *
from lib.models.model_factory import get_model
from lib.optimizers import RAdam
from lib import losses
from lib.decodes import decode
from lib.utils.vis import visualize
from lib.postprocess.nms import nms


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--name', default=None)
    parser.add_argument('--score_th', default=0.1, type=float)
    parser.add_argument('--nms', default=False, type=str2bool)
    parser.add_argument('--nms_th', default=0.1, type=float)
    parser.add_argument('--hflip', default=False, type=str2bool)
    parser.add_argument('--show', action='store_true')

    args = parser.parse_args()

    return args


def main():
    args = parse_args()

    with open('models/detection/%s/config.yml' % args.name, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    print('-'*20)
    for key in config.keys():
        print('%s: %s' % (key, str(config[key])))
    print('-'*20)

    cudnn.benchmark = True

    df = pd.read_csv('inputs/train.csv')
    img_paths = np.array('inputs/train_images/' + df['ImageId'].values + '.jpg')
    mask_paths = np.array('inputs/train_masks/' + df['ImageId'].values + '.jpg')
    labels = np.array([convert_str_to_labels(s) for s in df['PredictionString']])

    heads = OrderedDict([
        ('hm', 1),
        ('reg', 2),
        ('depth', 1),
    ])

    if config['rot'] == 'eular':
        heads['eular'] = 3
    elif config['rot'] == 'trig':
        heads['trig'] = 6
    elif config['rot'] == 'quat':
        heads['quat'] = 4
    else:
        raise NotImplementedError

    if config['wh']:
        heads['wh'] = 2

    # criterion = OrderedDict()
    # for head in heads.keys():
    #     criterion[head] = losses.__dict__[config[head + '_loss']]().cuda()

    pred_df = df.copy()
    pred_df['PredictionString'] = np.nan
    #
    # avg_meters = {'loss': AverageMeter()}
    # for head in heads.keys():
    #     avg_meters[head] = AverageMeter()

    kf = KFold(n_splits=config['n_splits'], shuffle=True, random_state=41)
    for fold, (train_idx, val_idx) in enumerate(kf.split(img_paths)):
        print('Fold [%d/%d]' %(fold + 1, config['n_splits']))

        train_img_paths, val_img_paths = img_paths[train_idx], img_paths[val_idx]
        train_mask_paths, val_mask_paths = mask_paths[train_idx], mask_paths[val_idx]
        train_labels, val_labels = labels[train_idx], labels[val_idx]

        val_set = Dataset(
            val_img_paths,
            val_mask_paths,
            val_labels,
            input_w=config['input_w'],
            input_h=config['input_h'],
            transform=None,
            lhalf=config['lhalf'])
        val_loader = torch.utils.data.DataLoader(
            val_set,
            batch_size=config['batch_size'],
            shuffle=False,
            num_workers=config['num_workers'],
            # pin_memory=True,
        )

        model = get_model(config['arch'], heads=heads,
                          head_conv=config['head_conv'],
                          num_filters=config['num_filters'],
                          dcn=config['dcn'],
                          gn=config['gn'], ws=config['ws'],
                          freeze_bn=config['freeze_bn'])
        model = model.cuda()

        model_path = 'models/detection/%s/model_%d.pth' % (config['name'], fold+1)
        if not os.path.exists(model_path):
            print('%s is not exists.' %model_path)
            continue
        model.load_state_dict(torch.load(model_path))

        model.eval()

        with torch.no_grad():
            pbar = tqdm(total=len(val_loader))
            for i, batch in enumerate(val_loader):
                input = batch['input'].cuda()
                mask = batch['mask'].cuda()
                hm = batch['hm'].cuda()
                reg_mask = batch['reg_mask'].cuda()

                output = model(input)

                # loss = 0
                # losses = {}
                # for head in heads.keys():
                #     losses[head] = criterion[head](output[head], batch[head].cuda(),
                #                                    mask if head == 'hm' else reg_mask)
                #     loss += losses[head]
                # losses['loss'] = loss
                #
                # avg_meters['loss'].update(losses['loss'].item(), input.size(0))
                # postfix = OrderedDict([('loss', avg_meters['loss'].avg)])
                # for head in heads.keys():
                #     avg_meters[head].update(losses[head].item(), input.size(0))
                #     postfix[head + '_loss'] = avg_meters[head].avg
                # pbar.set_postfix(postfix)

                if args.hflip:
                    output_hf = model(torch.flip(input, (-1,)))
                    output_hf['hm'] = torch.flip(output_hf['hm'], (-1,))
                    output_hf['reg'] = torch.flip(output_hf['reg'], (-1,))
                    output_hf['reg'][:, 0] = 1 - output_hf['reg'][:, 0]
                    output_hf['depth'] = torch.flip(output_hf['depth'], (-1,))
                    if config['rot'] == 'trig':
                        output_hf['trig'] = torch.flip(output_hf['trig'], (-1,))
                        yaw = torch.atan2(output_hf['trig'][:, 1], output_hf['trig'][:, 0])
                        yaw *= -1.0
                        output_hf['trig'][:, 0] = torch.cos(yaw)
                        output_hf['trig'][:, 1] = torch.sin(yaw)
                        roll = torch.atan2(output_hf['trig'][:, 5], output_hf['trig'][:, 4])
                        roll = rotate(roll, -np.pi)
                        roll *= -1.0
                        roll = rotate(roll, np.pi)
                        output_hf['trig'][:, 4] = torch.cos(roll)
                        output_hf['trig'][:, 5] = torch.sin(roll)

                    if config['wh']:
                        output_hf['wh'] = torch.flip(output_hf['wh'], (-1,))

                    # output['hm'] = (output['hm'] + output_hf['hm']) / 2
                    # output['reg'] = (output['reg'] + output_hf['reg']) / 2
                    # output['depth'] = (output['depth'] + output_hf['depth']) / 2
                    # if config['rot'] == 'trig':
                    #     output['trig'] = (output['trig'] + output_hf['trig']) / 2
                    # if config['wh']:
                    #     output['wh'] = (output['wh'] + output_hf['wh']) / 2

                    output['hm'] = 0.8 * output['hm'] + 0.2 * output_hf['hm']
                    output['reg'] = 0.8 * output['reg'] + 0.2 * output_hf['reg']
                    output['depth'] = 0.8 * output['depth'] + 0.2 * output_hf['depth']
                    if config['rot'] == 'trig':
                        output['trig'] = 0.8 * output['trig'] + 0.2 * output_hf['trig']
                    if config['wh']:
                        output['wh'] = 0.8 * output['wh'] + 0.2 * output_hf['wh']

                dets = decode(
                    config,
                    output['hm'],
                    output['reg'],
                    output['depth'],
                    eular=output['eular'] if config['rot'] == 'eular' else None,
                    trig=output['trig'] if config['rot'] == 'trig' else None,
                    quat=output['quat'] if config['rot'] == 'quat' else None,
                    wh=output['wh'] if config['wh'] else None,
                    mask=mask,
                )
                dets = dets.detach().cpu().numpy()

                for k, det in enumerate(dets):
                    if args.nms:
                        det = nms(det, dist_th=args.nms_th)
                    img_id = os.path.splitext(os.path.basename(batch['img_path'][k]))[0]
                    pred_df.loc[pred_df.ImageId == img_id, 'PredictionString'] = convert_labels_to_str(det[det[:, 6] > args.score_th, :7])

                    if args.show:
                        gt = batch['gt'].numpy()[k]

                        img = cv2.imread(batch['img_path'][k])
                        img_gt = visualize(img, gt[gt[:, -1] > 0])
                        img_pred = visualize(img, det[det[:, 6] > args.score_th])

                        plt.subplot(121)
                        plt.imshow(img_gt[..., ::-1])
                        plt.subplot(122)
                        plt.imshow(img_pred[..., ::-1])
                        plt.show()

                pbar.update(1)
            pbar.close()

        torch.cuda.empty_cache()

        if not config['cv']:
            break

    # print('loss: %f' %avg_meters['loss'].avg)

    name = '%s_%.2f' %(args.name, args.score_th)
    if args.nms:
        name += '_nms%.2f' %args.nms_th
    if args.hflip:
        name += '_hf'
    pred_df.to_csv('outputs/submissions/val/%s.csv' %name, index=False)
    print(pred_df.head())


if __name__ == '__main__':
    main()
