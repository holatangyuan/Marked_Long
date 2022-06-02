import argparse
import os
import random
import shutil
import sys
import string
import time
import warnings
import math
import numpy as np
import json

import torch
from pathlib import Path
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models

import torch.nn.functional as F


#from torch.utils.tensorboard import SummaryWriter

import models.vgg as models_vgg
import utils

# Baseline DivNorm
from models.baseline_divnorm import *
from models.baseline_f1 import *

# MarkedLong path: 
#                   /mnt/cube/projects/contour_integration/markedlong_full/negative
#                   /mnt/cube/projects/contour_integration/markedlong_full/positive

# Pathfinder path: 
#                   /mnt/cube/projects/contour_integration/pathfinder_full/curv_contour_length_9

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))


parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('-a', '--arch', metavar='ARCH', default='baseline_f1')
parser.add_argument('-d', '--div', metavar='DIV', default=0)                # 0 = No DivNorm, 1 = Yes DivNorm
parser.add_argument('-l', '--layers', default=3, type=int, 
                    help='only allow for 3, 5, and 7 layers input')         # 3, 5, or 7 layers
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=1024, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)',
                    dest='weight_decay')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--tensorboard-dir', default='runs', help='path where to save tensorboard')
parser.add_argument('--multiprocessing-distributed', action='store_true',
                    help='Use multi-processing distributed training to launch '
                         'N processes per node, which has N GPUs. This is the '
                         'fastest way to use PyTorch for either single node or '
                         'multi node data parallel training')

best_acc1 = 0
global_stats_file = None
"""NOTE:
If you are using NCCL backend, remember to set 
the following environment variables in your Ampere series GPU.

export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=ALL
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=1
export NCCL_SOCKET_IFNAME=lo
"""
seed = 56
torch.manual_seed(seed)
np.random.seed(seed)


def generate_rand_string(n):
  letters = string.ascii_lowercase
  str_rand = ''.join(random.choice(letters) for i in range(n))
  return str_rand


def main():
    args = parser.parse_args()
    while True:
        #windows path uses '\\'
        checkpoint_dir = Path("checkpoints_%s/%s" % (args.data.split("\\")[-1], generate_rand_string(6)))
        if not os.path.exists(checkpoint_dir):
            args.checkpoint_dir = checkpoint_dir
            break
    args.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    print("Experiment dir:", args.checkpoint_dir)
    if args.seed is not None:
        # random.seed(args.seed)
        torch.manual_seed(args.seed)
        np.random.seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
        
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)


def main_worker(gpu, ngpus_per_node, args):

    # writer = SummaryWriter(str(args.tensorboard_dir))

    iterator = utils.Iterator()

    global best_acc1
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    if args.rank == 0:
        stats_file = open(args.checkpoint_dir / 'stats.txt', 'a', buffering=1)
        global_stats_file = stats_file
        args.global_stats_file = global_stats_file
        print(' '.join(sys.argv))
        print(str(vars(args)), file=global_stats_file)
        print(' '.join(sys.argv), file=global_stats_file)

    # create model
    if 'imagenet_100' in args.data:
        num_classes = 100
    elif not args.arch == '':
        num_classes = 2
    else:
        num_classes = 1000
    
    if args.rank == 0:
        print("%s-way classification" % num_classes)

    # MODEL INSTANTIATION

    # if args.pretrained:
    #     print("=> using pre-trained model '{}'".format(args.arch))
    #     if "divnorm" in str(args.arch):
    #         model_name = str(args.arch)
    #         model = models_vgg.__dict__[model_name](pretrained=True, num_classes=num_classes)
    #     else:
    #         model = models_vgg.__dict__[args.arch](pretrained=True, num_classes=num_classes)
    # else:
    #     print("=> creating model '{}'".format(args.arch))
    #     model_name = str(args.arch)
    #     model = models_vgg.__dict__[args.arch](pretrained=False, num_classes=num_classes)

    if str(args.arch) == "baseline":
        div_norm_choice = bool(int(args.div))
        print("=> creating model: '{}' | DivNormLayers: {}".format(args.arch, div_norm_choice))
        model = BaselineDivNorm(div_norm_choice)
    elif str(args.arch) == "baseline_f1":
        div_norm_choice = bool(int(args.div))
        model = BaselineF1(divnorm=div_norm_choice, 
                            L3=(args.layers==3), L5=(args.layers==5), L7=(args.layers==7))
        if args.layers == 3:
            print('Creating a 3-layer convnet | DivNormLayers: {}'.format(div_norm_choice))
        elif args.layers == 5:
            print('Creating a 5-layer convnet | DivNormLayers: {}'.format(div_norm_choice))
        elif args.layers == 7:
            print('Creating a 7-layer convnet | DivNormLayers: {}'.format(div_norm_choice))

    elif str(args.arch) == "resnet50":
        model = models.resnet50(pretrained=False, num_classes=2)
    else:
        print("Non-Baseline Models Not Available")
        raise NotImplementedError

    if not torch.cuda.is_available():
        print('using CPU, this will be slow')
    elif args.distributed:
        print("Distributed data parallel across GPUs")
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            # When using a single GPU per process and per
            # DistributedDataParallel, we need to divide the batch size
            # ourselves based on the total number of GPUs we have
            args.batch_size = int(args.batch_size / ngpus_per_node)
            args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        else:
            model.cuda()
            # DistributedDataParallel will divide and allocate batch_size to all
            # available GPUs if device_ids are not set
            model = torch.nn.parallel.DistributedDataParallel(model)
    elif args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
        print("Set to GPU", args.gpu)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            print("Dataparallel enabled")
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()
            print("Dataparallel enabled")

    criterion = nn.CrossEntropyLoss(reduction='mean').cuda(args.gpu)

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            if args.gpu is None:
                checkpoint = torch.load(args.resume)
            else:
                # Map model to be loaded to specified single gpu.
                loc = 'cuda:{}'.format(args.gpu)
                checkpoint = torch.load(args.resume, map_location=loc)
            args.start_epoch = checkpoint['epoch']
            best_acc1 = checkpoint['best_acc1']
            if args.gpu is not None:
                # best_acc1 may be from a checkpoint from a different GPU
                best_acc1 = best_acc1.to(args.gpu)
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'val')

    # normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
    #                                  std=[0.229, 0.224, 0.225])

    train_dataset = datasets.ImageFolder(
        traindir,
        transforms.Compose([
            transforms.Resize(160),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            # normalize,
        ]))

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(160),
            # transforms.CenterCrop(160),
            transforms.ToTensor(),
            # normalize,
        ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)
    

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None
    print("Creating dataloader..")
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)
    print("Created dataloader..")
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        adjust_learning_rate(optimizer, epoch, args)
        if epoch == 0:
            if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                        and args.rank == 0):
                    state = dict(epoch=epoch + 1, model=model.state_dict(),
                                optimizer=optimizer.state_dict())
                    torch.save(state, '%s/checkpoint_%s.pth' % (args.checkpoint_dir, str(args.arch)))
        # train for one epoch
        acc1 = train(train_loader, model, criterion, optimizer, epoch, args, writer=None, iterator=iterator)
        val_acc1, val_acc5 = validate(val_loader, model, criterion, optimizer, epoch, args)
        # remember best acc@1 and save checkpoint
        is_best = val_acc1 > best_acc1
        best_acc1 = max(val_acc1, best_acc1)
    
        stats = dict(epoch=epoch,
                    lr_weights=optimizer.param_groups[0]['lr'],
                    val_acc1=val_acc1.item())
        if args.rank == 0:
            print(json.dumps(stats), file=global_stats_file)

        # writer.add_scalar("Train/Best_Acc1", best_acc1, epoch)
        state = {
                    'epoch': epoch + 1,
                    'arch': args.arch,
                    'state_dict': model.state_dict(),
                    'best_acc1_train': best_acc1,
                    'optimizer' : optimizer.state_dict(),
                }
        if epoch > (args.epochs - 10):
            # store only last ten epoch weights
            if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                    and args.rank == 0):
                state = dict(epoch=epoch + 1, arch=args.arch, model=model.state_dict(),
                         optimizer=optimizer.state_dict())
                torch.save(state, "%s/checkpoint_%s_epoch_%s.pth" % (args.checkpoint_dir, str(args.arch), epoch))
        else:
            if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                    and args.rank == 0):
                state = dict(epoch=epoch + 1, arch=args.arch, model=model.state_dict(),
                            optimizer=optimizer.state_dict())
                torch.save(state, '%s/checkpoint_%s.pth' % (args.checkpoint_dir, str(args.arch)))
        
        if is_best:
            if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                    and args.rank == 0):
                print("Saving best model, val_acc = %s" % (val_acc1.item()))
                state = dict(epoch=epoch + 1, model=model.state_dict(),
                            optimizer=optimizer.state_dict(), val_acc1=val_acc1.item())
                torch.save(state, '%s/checkpoint_best_%s.pth' % (args.checkpoint_dir, str(args.arch)))

        
def train(train_loader, model, criterion, optimizer, epoch, args, writer, iterator):
    del writer  # unused here
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, top1, top5],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()

        # Creates once at the beginning of training
    scaler = torch.cuda.amp.GradScaler()

    for i, (images, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        
        # Save These images onto Disk (~/DeSaLab/segmentation_benchmark/semseg/vis)
        #image_path = "./vis/images/image[{}].npy".format(i)
        #target_path = "./vis/targets/target[{}].npy".format(i)
        #np.save(image_path, np_image)
        #np.save(target_path, np_target)

        # adjust learning rate at every step with warmup + cosine LR decay
        # adjust_learning_rate_cosine(args, optimizer, train_loader, i + epoch * len(train_loader))

        if args.gpu is not None:
            images = images.cuda(args.gpu, non_blocking=True)
        if torch.cuda.is_available():
            target = target.cuda(args.gpu, non_blocking=True)

        # Casts operations to mixed precision
        optimizer.zero_grad()
        with torch.cuda.amp.autocast():
            output = model(images)
            loss = criterion(output, target)

            # RELEASE below lines for no nan loss training
            # loss = criterion(output.float(), target)
            
        # if torch.isnan(loss):
        #     import ipdb; ipdb.set_trace()
        #     print(output.min(), output.max(), 
        #           target.min(), target.max(), 
        #           loss)

        # measure accuracy and record loss
        acc1, acc5 = accuracy(output, target, topk=(1, 1))
        losses.update(loss.item(), images.size(0))
        top1.update(acc1[0], images.size(0))
        top5.update(acc5[0], images.size(0))

        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        
        if args.rank == 0:
            # Clamping parameters of divnorm to non-negative values
            if "divnorm" in str(args.arch):
                if args.multiprocessing_distributed:
                    div_conv_weight = model.module.features[2].div.weight.data
                    div_conv_weight = div_conv_weight.clamp(min=0.)
                    model.module.features[2].div.weight.data = div_conv_weight
                else:
                    div_conv_weight = model.features[2].div.weight.data
                    div_conv_weight = div_conv_weight.clamp(min=0.)
                    model.features[2].div.weight.data = div_conv_weight
            if "dalernn" in str(args.arch):
                if args.multiprocessing_distributed:
                    div_conv_weight = model.module.features[2].rnn_cell.div.weight.data
                    div_conv_weight = div_conv_weight.clamp(min=0.)
                    model.module.features[2].rnn_cell.div.weight.data = div_conv_weight
                else:
                    div_conv_weight = model.features[2].rnn_cell.div.weight.data
                    div_conv_weight = div_conv_weight.clamp(min=0.)
                    model.features[2].rnn_cell.div.weight.data = div_conv_weight

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            if args.rank == 0:
                stats = dict(epoch=epoch, step=i,
                            lr_weights=optimizer.param_groups[0]['lr'],
                            loss=loss.item(),
                            acc1=acc1.item())
                print(json.dumps(stats))
                print(json.dumps(stats), file=args.global_stats_file)

        # writer.add_scalar("Loss/train", loss.item(), iterator.train_step)
        # writer.add_scalar("Learning Rate", optimizer.param_groups[0]["lr"], iterator.train_step)
        # writer.flush()

        iterator.add_train()
    return top1.avg

def validate(val_loader, model, criterion, optimizer, epoch, args):
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, losses, top1, top5],
        prefix='Test: ')

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (images, target) in enumerate(val_loader):
            if args.gpu is not None:
                images = images.cuda(args.gpu, non_blocking=True)
            if torch.cuda.is_available():
                target = target.cuda(args.gpu, non_blocking=True)

            output = model(images)
            loss = criterion(output, target)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(output, target, topk=(1, 1))
            losses.update(loss.item(), images.size(0))
            top1.update(acc1[0], images.size(0))
            top5.update(acc5[0], images.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                progress.display(i)

        print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
            .format(top1=top1, top5=top5))

    return top1.avg, top5.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print('\t'.join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = '{:' + str(num_digits) + 'd}'
        return '[' + fmt + '/' + fmt.format(num_batches) + ']'


def adjust_learning_rate(optimizer, epoch, args):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def adjust_learning_rate_cosine(args, optimizer, loader, step):
    max_steps = args.epochs * len(loader)
    warmup_steps = 10 * len(loader)
    base_lr = args.batch_size / 256
    if step < warmup_steps:
        lr = base_lr * step / warmup_steps
    else:
        step -= warmup_steps
        max_steps -= warmup_steps
        q = 0.5 * (1 + math.cos(math.pi * step / max_steps))
        end_lr = base_lr * 0.001
        lr = base_lr * q + end_lr * (1 - q)
    optimizer.param_groups[0]['lr'] = lr * args.lr
    

def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':
    main()
