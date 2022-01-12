import os
import torch
import torch.nn.functional as F
from tqdm import tqdm
import logging
import numpy as np
from collections import OrderedDict
from torchmeta.modules import MetaModule
import time

from torchmeta.datasets.helpers import omniglot
#from torchmeta.datasets.helpers import miniimagenet
from torchmeta.utils.data import BatchMetaDataLoader
#from torchmeta.utils.gradient_based import gradient_update_parameters

from model import ConvolutionalNeuralNetwork
from utils import get_accuracy
from sam import SAM

from sam_folder.model.smooth_cross_entropy import smooth_crossentropy
from sam_folder.utility.bypass_bn import enable_running_stats, disable_running_stats
from sam_folder.model.wide_res_net import WideResNet
from sam_folder.utility.step_lr import StepLR

logger = logging.getLogger(__name__)


def gradient_update_parameters_new(model,
                               train_input,
                               train_target,
                               loss,
                               params=None,
                               step_size=0.5,
                               first_order=False, adaptive = False, rho = 0.05, sam_lower=False):
    """Update of the meta-parameters with one step of gradient descent on the
    loss function.

    Parameters
    ----------
    model : `torchmeta.modules.MetaModule` instance
        The model.

    loss : `torch.Tensor` instance
        The value of the inner-loss. This is the result of the training dataset
        through the loss function.

    params : `collections.OrderedDict` instance, optional
        Dictionary containing the meta-parameters of the model. If `None`, then
        the values stored in `model.meta_named_parameters()` are used. This is
        useful for running multiple steps of gradient descent as the inner-loop.

    step_size : int, `torch.Tensor`, or `collections.OrderedDict` instance (default: 0.5)
        The step size in the gradient update. If an `OrderedDict`, then the
        keys must match the keys in `params`.

    first_order : bool (default: `False`)
        If `True`, then the first order approximation of MAML is used.

    Returns
    -------
    updated_params : `collections.OrderedDict` instance
        Dictionary containing the updated meta-parameters of the model, with one
        gradient update wrt. the inner-loss.
    """
    if not isinstance(model, MetaModule):
        raise ValueError('The model must be an instance of `torchmeta.modules.'
                         'MetaModule`, got `{0}`'.format(type(model)))


    if params is None:
        params = OrderedDict(model.meta_named_parameters())
    key_list = params.keys()
    items_list = params.values()

    grads = torch.autograd.grad(loss,
                                params.values(),
                                create_graph=not first_order)

    if sam_lower:
        #print(1)
        params_list = list(params.values())


        ###start new code
        gradnorm = grad_norm(params_list, grads, adaptive)

        scale = rho / (gradnorm + 1e-12)
        #print('momin2: ', type(scale))

        l = list(range(len(grads)))
        old_p = []
        for i in l:
            old_p.append(torch.zeros_like(params_list[i]))

        for i in l:
            #if grads[i] is None: continue
            #old_p[i] = params_list[i].data.clone().requires_grad_(True)
            #old_p[i] = torch.tensor(params_list[i].data, requires_grad=True)
            e_w = (torch.pow(params_list[i], 2) if adaptive else 1.0) * grads[i] * scale.to(params_list[i])
            # print('e_2w: ', e_w[0:2])
            # print('val1: ', params_list[i][0:2])
            params_list[i] = params_list[i].add(e_w)  # climb to the local maximum "w + e(w)"


            # print('val2: ', params_list[i][0:2])
            # print('val3: ', old_p[i][0:2])


        #params_old = OrderedDict(zip(key_list, old_p))
        params_new = OrderedDict(zip(key_list, params_list))
        # print('new params', list(params_new.keys())[0])
        # print('old params', list(params.keys())[0])


        train_logit = model(train_input, params=params_new)
        inner_loss = F.cross_entropy(train_logit, train_target)
        model.zero_grad()
        grads_new = torch.autograd.grad(inner_loss,
                                    params_new.values(),
                                    create_graph=not first_order)

        updated_params = OrderedDict()
        if isinstance(step_size, (dict, OrderedDict)):
            for (name, param), grad in zip(params.items(), grads_new):
                updated_params[name] = param - step_size[name] * grad

        else:
            for (name, param), grad in zip(params.items(), grads_new):
                updated_params[name] = param - step_size * grad
    ###end new code

    else:
        #print(2)
        updated_params = OrderedDict()
        if isinstance(step_size, (dict, OrderedDict)):
            for (name, param), grad in zip(params.items(), grads):
                updated_params[name] = param - step_size[name] * grad

        else:
            for (name, param), grad in zip(params.items(), grads):
                updated_params[name] = param - step_size * grad

    return updated_params

def grad_norm(params_list, grads, adaptive):
    #print('momin3', len(list(params.values())))import torch

    shared_device = params_list[0].device  # put everything on the same device, in case of model parallelism
    l = list(range(len(grads)))
    #print('l: ', l)
    norm = torch.norm(
                torch.stack([
                    ((torch.abs(params_list[i]) if adaptive else 1.0) * grads[i]).norm(p=2).to(shared_device)
                     for i in l
                    if grads is not None
                ]),
                p=2
           )
    return norm


def train(args):
    logger.warning('This script is an example to showcase the MetaModule and '
                   'data-loading features of Torchmeta, and as such has been '
                   'very lightly tested. For a better tested implementation of '
                   'Model-Agnostic Meta-Learning (MAML) using Torchmeta with '
                   'more features (including multi-step adaptation and '
                   'different datasets), please check `https://github.com/'
                   'tristandeleu/pytorch-maml`.')

    dataset = omniglot(args.folder,
                       shots=args.num_shots,
                       ways=args.num_ways,
                       shuffle=True,
                       test_shots=15,
                       seed=0,
                       meta_val=False,
                       meta_test = True,
                       meta_train=False,
                       download=args.download,
                       )

    dataloader = BatchMetaDataLoader(dataset,
                                     batch_size=args.batch_size,
                                     shuffle=True,
                                     num_workers=args.num_workers)

    model = ConvolutionalNeuralNetwork(1 ,args.num_ways, hidden_size=args.hidden_size, final_layer_size=64)
    model.to(device=args.device)
    model.train()
    # meta_optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    base_optimizer = torch.optim.Adam
    # meta_optimizer = SAM(model.parameters(), base_optimizer, rho=0.05,
    #                         adaptive=args.adap, lr=1e-6)
    #scheduler = StepLR(meta_optimizer, 1e-6, args.num_batches)
    
    print('rho: ', args.rho)
    print('SAM_lower: ', args.SAM_lower)
    print('Adaptive: ', args.adap)
    meta_optimizer = SAM(model.parameters(), base_optimizer, rho=args.rho,
                            adaptive=args.adap, lr=1e-3)
    #scheduler = StepLR(meta_optimizer, 1e-3, args.num_batches)

    loss_acc_time_results = np.zeros((args.num_batches+1, 2))

    # Training loop
    start_time = time.time()
    with tqdm(dataloader, total=args.num_batches) as pbar:
        for batch_idx, batch in enumerate(pbar):
            enable_running_stats(model)

            model.zero_grad()

            train_inputs, train_targets = batch['train']
            #print(train_inputs.size())
            train_inputs = train_inputs.to(device=args.device)
            train_targets = train_targets.to(device=args.device)
            #print('batch: ', batch.keys())

            test_inputs, test_targets = batch['test']
            test_inputs = test_inputs.to(device=args.device)
            test_targets = test_targets.to(device=args.device)

            # val_inputs, val_targets = batch['validation']
            # val_inputs = val_inputs.to(device=args.device)
            # val_targets = val_targets.to(device=args.device)
            # print('val: ', val_inputs.size())

            outer_loss = torch.tensor(0., device=args.device)
            outer_loss2 = torch.tensor(0., device=args.device)
            #outer_temp =  torch.tensor(0., device=args.device)
            #accuracy = torch.tensor(0., device=args.device)
            for task_idx, (train_input, train_target, test_input,
                    test_target) in enumerate(zip(train_inputs, train_targets,
                    test_inputs, test_targets)):
                train_logit = model(train_input)
                inner_loss = F.cross_entropy(train_logit, train_target)
                #inner_loss = smooth_crossentropy(train_logit, train_target, smoothing=0.000).mean()

                model.zero_grad()
                # params = gradient_update_parameters(model, inner_loss, step_size=args.step_size,
                #                                     first_order=args.first_order)
                                
                params = gradient_update_parameters_new(model,train_input, train_target, inner_loss, step_size=args.step_size,
                                                    first_order=args.first_order, adaptive = args.adap, 
                                                    rho = args.rho, sam_lower = args.SAM_lower)

                test_logit = model(test_input, params=params)
                outer_loss += F.cross_entropy(test_logit, test_target)
                #outer_loss += smooth_crossentropy(test_logit, test_target, smoothing=0.00).mean()
             
                # with torch.no_grad():
                #     accuracy += get_accuracy(test_logit, test_target)
                #     #scheduler(batch_idx)

            outer_loss.div_(args.batch_size)            
            #accuracy.div_(args.batch_size)

            outer_loss.backward()
            meta_optimizer.first_step(zero_grad=True)
            
            disable_running_stats(model)
            accuracy2 = torch.tensor(0., device=args.device)

            for task_idx, (train_input, train_target, test_input,
                    test_target) in enumerate(zip(train_inputs, train_targets,
                    test_inputs, test_targets)):
                train_logit = model(train_input)
                inner_loss = F.cross_entropy(train_logit, train_target)
                #inner_loss = smooth_crossentropy(train_logit, train_target, smoothing=0.00).mean()

                model.zero_grad()

                # params = gradient_update_parameters(model, inner_loss, step_size=args.step_size,
                #                                     first_order=args.first_order)                     
                params = gradient_update_parameters_new(model,train_input, train_target, inner_loss, step_size=args.step_size,
                                                    first_order=args.first_order, adaptive = args.adap, rho = args.rho, sam_lower = args.SAM_lower)

                test_logit = model(test_input, params=params)
                outer_loss2 += F.cross_entropy(test_logit, test_target)
                #outer_loss2 += smooth_crossentropy(test_logit, test_target, smoothing=0.00).mean()
                
                with torch.no_grad():
                    accuracy2 += get_accuracy(test_logit, test_target)
                    #scheduler(batch_idx)

            outer_loss2.div_(args.batch_size)
            accuracy2.div_(args.batch_size)

            outer_loss2.backward()
            meta_optimizer.second_step(zero_grad=True)

            print('test_loss: ', outer_loss2)

            loss_acc_time_results[batch_idx, 0] = accuracy2.item()
            loss_acc_time_results[batch_idx, 1] = outer_loss2.item()

            pbar.set_postfix(accuracy='{0:.4f}'.format(accuracy2.item()))
            if batch_idx >= args.num_batches:
                break
    

    print('Training finished, took {:.2f}s'.format(time.time() - start_time))
    print(loss_acc_time_results)

    if args.SAM_lower:
        file_name = 'results_BiSAM_{}_omniglot_20way_1shot_both_t{}.npy'.format(args.rho, args.trial)
    else:
        file_name = 'results_BiSAM_{}_omniglot_20way_1shot_upper_t{}.npy'.format(args.rho, args.trial)
    file_addr = os.path.join('./save_results_omni', file_name)
    with open(file_addr, 'wb') as f:
            np.save(f, loss_acc_time_results)   

    # Save model
    if args.output_folder is not None:
        filename = os.path.join(args.output_folder, 'maml_omniglot_'
            '{0}shot_{1}way.th'.format(args.num_shots, args.num_ways))
        with open(filename, 'wb') as f:
            state_dict = model.state_dict()
            torch.save(state_dict, f)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser('Model-Agnostic Meta-Learning (MAML)')

    parser.add_argument('folder', type=str, help='Path to the folder the data is downloaded to.')
    parser.add_argument('--num-shots', type=int, default=5, help='Number of examples per class (k in "k-shot", default: 5).')
    parser.add_argument('--num-ways', type=int, default=5, help='Number of classes per task (N in "N-way", default: 5).')
    parser.add_argument('--first-order', action='store_true', help='Use the first-order approximation of MAML.')
    parser.add_argument('--step-size', type=float, default=0.1, help='Step-size for the gradient step for adaptation (default: 0.4).')
    parser.add_argument('--SAM_lower', type=bool, default=True, help='Apply SAM on inner MAML update')
    parser.add_argument('--rho', type=float, default=0.00005, help='radius rho for SAM')
    parser.add_argument('--adap', type=bool, default=False, help='Apply ASAM (adaptive SAM) on MAML')
    parser.add_argument('--hidden-size', type=int, default=64, help='Number of channels for each convolutional layer (default: 64).')
    parser.add_argument('--output-folder', type=str, default=None, help='Path to the output folder for saving the model (optional).')
    parser.add_argument('--batch-size', type=int, default=16, help='Number of tasks in a mini-batch of tasks (default: 16).')
    parser.add_argument('--num-batches', type=int, default=1000, help='Number of batches the model is trained over (default: 100).')
    parser.add_argument('--num-workers', type=int, default=1, help='Number of workers for data loading (default: 1).')
    parser.add_argument('--download', action='store_true', help='Download the omniglot dataset in the data folder.')
    parser.add_argument('--use-cuda', action='store_true', help='Use CUDA if available.')
    parser.add_argument('--trial', type=int, default=2, help='Use CUDA if available.')

    args = parser.parse_args()
    args.device = torch.device("cuda:{}".format(args.trial) if args.use_cuda and torch.cuda.is_available() else "cpu")
    print('GPU available: ', torch.cuda.is_available())
    train(args)


