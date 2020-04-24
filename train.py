import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import argparse, time
from tqdm import tqdm
import math
from model.translator import Translator
from translate import translation_score

def patch_source(source):
    source = source.transpose(0, 1)
    return source


def patch_target(target):
    target = target.transpose(0, 1)
    gold = target[:, 1:]
    target = target[:, :-1]
    return target, gold


def calculate_metrics(prediction, gold, trg_pad_idx, smoothing=False):
    loss = compute_loss(prediction, gold, trg_pad_idx, smoothing=smoothing)
    prediction = prediction.max(1)[1]
    non_pad_mask = gold.ne(trg_pad_idx)
    num_correct = prediction.eq(gold).masked_select(non_pad_mask).sum().item()
    num_words = non_pad_mask.sum().item()
    return loss, num_correct, num_words


def compute_loss(prediction, gold, trg_pad_idx, smoothing=False):
    ''' Calculate cross entropy loss, apply label smoothing if needed. '''
    if smoothing:
        eps = 0.1
        n_class = prediction.size(1)
        one_hot = torch.zeros_like(prediction).scatter(1, gold.view(-1, 1), 1)
        one_hot = one_hot * (1 - eps) + (1 - one_hot) * eps / (n_class - 1)
        log_prb = F.log_softmax(prediction, dim=1)
        non_pad_mask = gold.ne(trg_pad_idx)
        loss = -(one_hot * log_prb).sum(dim=1)
        loss = loss.masked_select(non_pad_mask).sum()  # average later
    else:
        loss = F.cross_entropy(prediction, gold, ignore_index=trg_pad_idx, reduction='sum')
    return loss


def run_one_epoch(model, data, args, device,TRG, optimizer=None, smoothing=False, bleu=False):
    ''' Epoch operation in training phase'''
    training = optimizer is not None
    total_loss, total_num_words, total_num_correct_words, total_bleu,total_sentence = 0, 0, 0, 0,0
    if training:
        desc = '  - (Training)   '
        model.train()
    else:
        desc = '  - (Validation) '
        model.eval()
    if bleu:
        translator = Translator(
            model=model,
            beam_size=args.beam_size,
            max_seq_len=args.max_seq_len,
            src_pad_idx=args.src_pad_idx,
            trg_pad_idx=args.trg_pad_idx,
            trg_bos_idx=args.trg_bos_idx,
            trg_eos_idx=args.trg_eos_idx,
            device=device).to(device)
    for batch in tqdm(data, mininterval=0.5, desc=desc, leave=False):
        # prepare data
        source_sequence = patch_source(batch.src).to(device)
        target_sequence, gold = map(lambda x: x.to(device), patch_target(batch.trg))
        # forward pass
        if training:
            optimizer.zero_grad()
        if bleu:
            pred_seq, ends = translator.translate_sentence(source_sequence)
            bleu_scores = translation_score(pred_seq, ends, gold, TRG)
            total_bleu += bleu_scores[0]
            # print(bleu_scores[0])
            total_sentence += bleu_scores[1]
        prediction = model(source_sequence, target_sequence)
        output = model.generator(prediction)
        output = output.view(-1, output.size(-1))
        # backward pass and update parameters
        loss, num_correct, num_words = calculate_metrics(
            output, gold.contiguous().view(-1), args.trg_pad_idx, smoothing=smoothing
        )
        if training:
            loss.backward()
            optimizer.step_and_update_lr()
        total_num_words += num_words
        total_num_correct_words += num_correct
        total_loss += loss.item()
    if total_num_words != 0:
        loss_per_word = total_loss/total_num_words
        accuracy = total_num_correct_words / total_num_words
        if bleu:
            bleu_score = total_bleu / total_sentence
            print('current BLEU score: ',bleu_score)
    else:
        loss_per_word = 0
        accuracy = 0
    return loss_per_word, accuracy


def train(model, training_data, validation_data, optimizer, args, device,SRC,TRG,bleu_freq):
    ''' Start training '''
    log_train_file, log_valid_file = None, None
    "We can optionally log the training and validation processes."
    if args.log:
        log_train_file = f'{args.log}.train.log'
        log_valid_file = f'{args.log}.valid.log'
        print(f'[Info] Training performance will be written to file: {log_train_file} and {log_valid_file}')
        with open(log_train_file, 'w') as log_tf, open(log_valid_file, 'w') as log_vf:
            log_tf.write('epoch,loss,ppl,accuracy\n')
            log_vf.write('epoch,loss,ppl,accuracy\n')

    "Utility function for printing performance at a given time."
    def print_performances(header, loss, accu, start_time):
        print('  - {header:12} ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, '\
              'elapse: {elapse:3.3f} min'.format(
                  header=f"({header})", ppl=math.exp(min(loss, 100)),
                  accu=100*accu, elapse=(time.time()-start_time)/60))

    validation_losses = []
    for epoch_number in range(args.epoch):
        print('[ Epoch', epoch_number, ']')
        start_time = time.time()
        training_loss, training_accuracy = run_one_epoch(
            model,
            training_data,
            args,
            device,
            TRG,
            optimizer=optimizer,
            smoothing=args.label_smoothing
        )
        print_performances('Training', training_loss, training_accuracy, start_time)
        # start = time.time()
        cal_bleu=False
        if epoch_number % bleu_freq == 0 and epoch_number != 0:
            cal_bleu=True
        validation_loss, validation_accuracy = run_one_epoch(
            model,
            validation_data,
            args,
            device,
            TRG,
            optimizer=None,
            bleu=cal_bleu
        )
        print_performances('Validation', validation_loss, validation_accuracy, start_time)
        validation_losses += [validation_loss]
        checkpoint = {'epoch': epoch_number, 'settings': args, 'model': model.state_dict()}
        "Optionally save the model."
        if args.save_model:
            if args.save_mode == 'all':
                model_name = f'{args.save_model}_accu_{validation_accuracy:3.3f}.chkpt'
                torch.save(checkpoint, model_name)
            elif args.save_mode == 'best':
                model_name = f'{args.save_model}.chkpt'
                if validation_loss <= min(validation_losses):
                    torch.save(checkpoint, model_name)
                    print('    - [Info] The checkpoint file has been updated.')

        "Optionally log the training/validation step."
        if log_train_file and log_valid_file:
            with open(log_train_file, 'a') as log_tf, open(log_valid_file, 'a') as log_vf:
                results = '{epoch},{loss: 8.5f},{ppl: 8.5f},{accu:3.3f}\n'.format(
                    epoch=epoch_number,
                    loss=training_loss,
                    ppl=math.exp(min(training_loss, 100)),
                    accu=100*training_accuracy
                )
                log_tf.write(results)
                log_vf.write(results)
