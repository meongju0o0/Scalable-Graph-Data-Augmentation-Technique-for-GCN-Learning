import argparse
import socket
import time

import dgl
import numpy as np
import torch as th
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from training.evaluation import compute_acc, evaluate
from training.model import DistSAGE
from training.loss import HLoss, XeLoss, JensenShannon

from mh_aug import mh_aug
from common.create_batch import AugDataLoader
from common.config import CONFIG


def run(args, device, data):
    """
    Train and evaluate DistSAGE.

    Parameters
    ----------
    args : argparse.Args
        Arguments for train and evaluate.
    device : torch.Device
        Target device for train and evaluate.
    data : Packed Data
        This includes train/val/test IDs, feature dimension,
        number of classes, graph.
    """
    # Initial var declare and copy for augmentation training
    train_nid, val_nid, test_nid, in_feats, n_classes, org_g = data
    org_g.ndata["ones"] = th.ones(org_g.num_nodes())
    cur_g = org_g

    # Declare dataloader
    sampler = dgl.dataloading.NeighborSampler([int(fanout) for fanout in args.fan_out.split(",")])
    dataloader = AugDataLoader(org_g, train_nid, sampler, batch_size=args.batch_size, shuffle=True, drop_last=False)

    # Declare Training Methods
    model = DistSAGE(
        in_feats,
        args.num_hidden,
        n_classes,
        args.num_layers,
        F.relu,
        args.dropout,
    )
    model = model.to(device)
    if args.num_gpus == 0:
        model = th.nn.parallel.DistributedDataParallel(model)
    else:
        model = th.nn.parallel.DistributedDataParallel(model, device_ids=[device], output_device=device)
    hard_xe_loss_op = nn.CrossEntropyLoss()
    soft_xe_loss_op = XeLoss()
    h_loss_op = HLoss()
    js_loss_op = JensenShannon()
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.decay)

    # Training loop.
    iter_tput = []
    epoch = 0
    epoch_time = []
    test_acc = 0.0
    for _ in range(args.num_epochs):
        epoch += 1
        tic = time.time()
        # Various time statistics.
        sample_time = 0
        forward_time = 0
        backward_time = 0
        update_time = 0
        num_seeds = 0
        num_inputs = 0
        start = time.time()
        step_time = []

        with (model.join()):
            prev_g = cur_g
            cur_g, kl_loss_opt = mh_aug(args, org_g, prev_g, model, dataloader, device)
            for step, (input_nodes, seeds, org_blocks, prev_blocks, cur_blocks) in \
                enumerate(dataloader.__iter__(prev_g, cur_g)):
                # input_nodes: src nodes, i.e. whole nodes
                # seeds: dst nodes
                # blocks: Message Flow Graph

                # Declare time variable to calculate computing time
                tic_step = time.time()
                sample_time += tic_step - start

                # Slice feature and label.
                batch_inputs = org_g.ndata["features"][input_nodes]
                batch_labels = org_g.ndata["labels"][seeds].long()
                num_seeds += len(blocks[-1].dstdata[dgl.NID])
                num_inputs += len(blocks[0].srcdata[dgl.NID])

                # Move to target device.
                blocks = [block.to(device) for block in blocks]
                batch_inputs = batch_inputs.to(device)
                batch_labels = batch_labels.to(device)

                # Compute loss and prediction.
                start = time.time()

                batch_pred = model(blocks, batch_inputs)
                batch_prev_pred = model(prev_blocks, batch_inputs)
                batch_cur_pred = model(cur_blocks, batch_inputs)

                forward_end = time.time()

                loss_XE = hard_xe_loss_op(batch_prev_pred, batch_labels)
                if args.option_loss == 0:
                    loss_KL = soft_xe_loss_op(batch_prev_pred, batch_cur_pred)
                else:
                    if kl_loss_opt:
                        loss_KL = js_loss_op(batch_prev_pred.detach(), batch_cur_pred)
                    else:
                        loss_KL = js_loss_op(batch_prev_pred, batch_cur_pred.detach())
                loss_H = h_loss_op(batch_pred)

                total_loss = loss_XE + args.kl * loss_KL + args.h * loss_H

                optimizer.zero_grad()
                total_loss.backward()

                # Calculate computing time
                compute_end = time.time()
                forward_time += forward_end - start
                backward_time += compute_end - forward_end

                optimizer.step()
                update_time += time.time() - compute_end

                step_t = time.time() - tic_step
                step_time.append(step_t)
                iter_tput.append(len(blocks[-1].dstdata[dgl.NID]) / step_t)
                acc = compute_acc(batch_pred, batch_labels)
                gpu_mem_alloc = (
                    th.cuda.max_memory_allocated() / 1000000
                    if th.cuda.is_available()
                    else 0
                )
                sample_speed = np.mean(iter_tput[-args.log_every :])
                mean_step_time = np.mean(step_time[-args.log_every :])
                print(
                    f"Part {org_g.rank()} | Epoch {epoch:05d} | Step {step:05d}"
                    f" | Loss {total_loss.item():.4f} | Train Acc {acc.item():.4f}"
                    f" | Speed (samples/sec) {sample_speed:.4f}"
                    f" | GPU {gpu_mem_alloc:.1f} MB | "
                    f"Mean step time {mean_step_time:.3f} s"
                )
                start = time.time()

        toc = time.time()
        print(
            f"Part {org_g.rank()}, Epoch Time(s): {toc - tic:.4f}, "
            f"sample+data_copy: {sample_time:.4f}, forward: {forward_time:.4f},"
            f" backward: {backward_time:.4f}, update: {update_time:.4f}, "
            f"#seeds: {num_seeds}, #inputs: {num_inputs}"
        )
        epoch_time.append(toc - tic)

        if epoch % args.eval_every == 0 or epoch == args.num_epochs:
            start = time.time()
            val_acc, test_acc = evaluate(
                model.module,
                org_g,
                org_g.ndata["features"],
                org_g.ndata["labels"],
                val_nid,
                test_nid,
                args.batch_size_eval,
                device,
            )
            print(
                f"Part {org_g.rank()}, Val Acc {val_acc:.4f}, "
                f"Test Acc {test_acc:.4f}, time: {time.time() - start:.4f}"
            )

    return np.mean(epoch_time[-int(args.num_epochs * 0.8) :]), test_acc


def main(args):
    """
    Main function.
    """
    host_name = socket.gethostname()
    print(f"{host_name}: Initializing DistDGL.")
    dgl.distributed.initialize(args.ip_config)
    print(f"{host_name}: Initializing PyTorch process group.")
    th.distributed.init_process_group(backend=args.backend)
    print(f"{host_name}: Initializing DistGraph.")
    g = dgl.distributed.DistGraph(args.graph_name, part_config=args.part_config)
    print(f"Rank of {host_name}: {g.rank()}")

    # Split train/val/test IDs for each trainer.
    pb = g.get_partition_book()
    if "trainer_id" in g.ndata:
        train_nid = dgl.distributed.node_split(
            g.ndata["train_mask"],
            pb,
            force_even=True,
            node_trainer_ids=g.ndata["trainer_id"],
        )
        val_nid = dgl.distributed.node_split(
            g.ndata["val_mask"],
            pb,
            force_even=True,
            node_trainer_ids=g.ndata["trainer_id"],
        )
        test_nid = dgl.distributed.node_split(
            g.ndata["test_mask"],
            pb,
            force_even=True,
            node_trainer_ids=g.ndata["trainer_id"],
        )
    else:
        train_nid = dgl.distributed.node_split(g.ndata["train_mask"], pb, force_even=True)
        val_nid = dgl.distributed.node_split(g.ndata["val_mask"], pb, force_even=True)
        test_nid = dgl.distributed.node_split(g.ndata["test_mask"], pb, force_even=True)
    local_nid = pb.partid2nids(pb.partid).detach().numpy()
    num_train_local = len(np.intersect1d(train_nid.numpy(), local_nid))
    num_val_local = len(np.intersect1d(val_nid.numpy(), local_nid))
    num_test_local = len(np.intersect1d(test_nid.numpy(), local_nid))
    print(
        f"part {g.rank()}, train: {len(train_nid)} (local: {num_train_local}), "
        f"val: {len(val_nid)} (local: {num_val_local}), "
        f"test: {len(test_nid)} (local: {num_test_local})"
    )
    del local_nid
    if args.num_gpus == 0:
        device = th.device("cpu")
    else:
        dev_id = g.rank() % args.num_gpus
        device = th.device("cuda:" + str(dev_id))
    n_classes = args.n_classes
    if n_classes == 0:
        labels = g.ndata["labels"][np.arange(g.num_nodes())]
        n_classes = len(th.unique(labels[th.logical_not(th.isnan(labels))]))
        del labels
    print(f"Number of classes: {n_classes}")

    # Pack data.
    in_feats = g.ndata["features"].shape[1]
    data = train_nid, val_nid, test_nid, in_feats, n_classes, g

    # Train and evaluate.
    epoch_time, test_acc = run(args, device, data)
    print(
        f"Summary of node classification(GraphSAGE): GraphName "
        f"{args.graph_name} | TrainEpochTime(mean) {epoch_time:.4f} "
        f"| TestAccuracy {test_acc:.4f}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distributed GraphSAGE")
    parser.add_argument("--graph_name", type=str,
        help="graph name")
    parser.add_argument("--ip_config", type=str,
        help="The file for IP configuration")
    parser.add_argument("--part_config", type=str,
        help="The path to the partition config file")
    parser.add_argument("--n_classes", type=int, default=0,
        help="the number of classes")
    parser.add_argument("--backend", type=str, default="gloo",
        help="pytorch distributed backend")
    parser.add_argument("--num_gpus", type=int, default=0,
        help="the number of GPU device. Use 0 for CPU training")
    parser.add_argument("--num_epochs", type=int, default=20)
    parser.add_argument("--num_hidden", type=int, default=16)
    parser.add_argument("--num_layers", type=int, default=2)
    parser.add_argument("--fan_out", type=str, default="10,25")
    parser.add_argument("--batch_size", type=int, default=1000)
    parser.add_argument("--batch_size_eval", type=int, default=100000)
    parser.add_argument("--log_every", type=int, default=20)
    parser.add_argument("--eval_every", type=int, default=5)
    parser.add_argument("--lr", type=float, default=0.003)
    parser.add_argument("--decay", type=float, default=0.0005)
    parser.add_argument("--dropout", type=float, default=0.5)
    parser.add_argument("--local_rank", type=int, help="get rank of the process")
    parser.add_argument("--pad-data", default=False, action="store_true",
        help="Pad train nid to the same length across machine, to ensure num of batches to be the same.")
    args = argparse.Namespace(**CONFIG)
    args = parser.parse_args()
    print(f"Arguments: {args}")
    main(args)
