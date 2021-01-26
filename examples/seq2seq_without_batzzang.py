# used codes from https://pytorch.org/tutorials/beginner/chatbot_tutorial.html
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
import random
import itertools
import time
import argparse
from load_and_trim_data import EOS_token, PAD_token, SOS_token, MAX_LENGTH, loadPrepareData, trimRareWords
from create_formatted_data_file import create_formatted_data_file


USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda" if USE_CUDA else "cpu")


def indexesFromSentence(voc, sentence):
    return [voc.word2index[word] for word in sentence.split(' ')] + [EOS_token]


def zeroPadding(l, fillvalue=PAD_token):
    return list(itertools.zip_longest(*l, fillvalue=fillvalue))


def binaryMatrix(l):
    m = []
    for i, seq in enumerate(l):
        m.append([])
        for token in seq:
            if token == PAD_token:
                m[i].append(0)
            else:
                m[i].append(1)
    return m


# Returns padded input sequence tensor and lengths
def inputVar(l, voc):
    indexes_batch = [indexesFromSentence(voc, sentence) for sentence in l]
    lengths = torch.tensor([len(indexes) for indexes in indexes_batch])
    padList = zeroPadding(indexes_batch)
    padVar = torch.LongTensor(padList).to(device)
    return padVar, lengths


# Returns padded target sequence tensor, padding mask, and max target length
def outputVar(l, voc):
    indexes_batch = [indexesFromSentence(voc, sentence) for sentence in l]
    max_target_len = max([len(indexes) for indexes in indexes_batch])
    padList = zeroPadding(indexes_batch)
    mask = binaryMatrix(padList)
    mask = torch.BoolTensor(mask).to(device)
    padVar = torch.LongTensor(padList).to(device)
    return padVar, mask, max_target_len


# Returns all items for a given batch of pairs
def batch2TrainData(voc, pair_batch):
    pair_batch.sort(key=lambda x: len(x[0].split(" ")), reverse=True)
    input_batch, output_batch = [], []
    for pair in pair_batch:
        input_batch.append(pair[0])
        output_batch.append(pair[1])
    inp, lengths = inputVar(input_batch, voc)
    output, mask, max_target_len = outputVar(output_batch, voc)
    return inp, lengths, output, mask, max_target_len


# Luong attention layer
class Attn(nn.Module):
    def __init__(self, hidden_size):
        super(Attn, self).__init__()
        self.hidden_size = hidden_size

    def dot_score(self, hidden, encoder_output):
        return torch.sum(hidden * encoder_output, dim=2)

    def forward(self, hidden, encoder_outputs):
        # Calculate the attention weights (energies) based on the given method
        attn_energies = self.dot_score(hidden, encoder_outputs)

        # Transpose max_length and batch_size dimensions
        attn_energies = attn_energies.t()

        # Return the softmax normalized probability scores (with added dimension)
        return F.softmax(attn_energies, dim=1).unsqueeze(1)


class GRUSeq2SeqWithAttention(nn.Module):
    def __init__(self, vocab_num, hidden_size, layer_num, dropout):
        super(GRUSeq2SeqWithAttention, self).__init__()
        self.embedding = nn.Embedding(vocab_num, hidden_size)
        self.dropout = nn.Dropout(dropout)
        self.encoder_gru = nn.GRU(hidden_size, hidden_size, layer_num,
                                  dropout=(0 if layer_num == 1 else dropout), bidirectional=True)
        self.decoder_gru = nn.GRU(hidden_size, hidden_size, layer_num,
                                  dropout=(0 if layer_num == 1 else dropout), bidirectional=False)

        self.attn = Attn(hidden_size)
        self.concat = nn.Linear(hidden_size * 2, hidden_size)
        self.out = nn.Linear(hidden_size, vocab_num)
        self.hidden_size = hidden_size
        self.layer_num = layer_num

    def encode(self, input_variable, input_lengths):
        embedded = self.embedding(input_variable)
        embedded = self.dropout(embedded)
        # Lengths for rnn packing should always be on the cpu
        input_lengths = input_lengths.to("cpu")
        # Pack padded batch of sequences for RNN module
        packed = nn.utils.rnn.pack_padded_sequence(embedded, input_lengths)
        encoder_outputs, encoder_hidden = self.encoder_gru(packed, None)
        # Unpack padding
        encoder_outputs, _ = nn.utils.rnn.pad_packed_sequence(encoder_outputs)
        encoder_outputs = encoder_outputs[:, :, :self.hidden_size] + encoder_outputs[:, :, self.hidden_size:]
        return encoder_outputs, encoder_hidden

    def decode(self, decoder_input, decoder_hidden, encoder_outputs):
        # Note: we run this one step (word) at a time
        # Get embedding of current input word
        embedded = self.embedding(decoder_input)
        embedded = self.dropout(embedded)
        # Forward through unidirectional GRU
        gru_output, hidden = self.decoder_gru(embedded, decoder_hidden)
        # Calculate attention weights from the current GRU output
        attn_weights = self.attn(gru_output, encoder_outputs)
        # Multiply attention weights to encoder outputs to get new "weighted sum" context vector
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))
        # Concatenate weighted context vector and GRU output using Luong eq. 5
        context = context.squeeze(1)
        concat_input = torch.cat((gru_output.squeeze(0), context), 1)
        concat_output = torch.tanh(self.concat(concat_input))
        # Predict next word using Luong eq. 6
        output = self.out(concat_output)
        output = F.softmax(output, dim=1)
        # Return output and final hidden state
        return output, hidden

    def forward_no_teacher_forcing(self, input_variable, input_lengths, target_variable, mask, max_target_len):
        encoder_outputs, encoder_hidden = self.encode(input_variable, input_lengths)
        batch_size = len(input_lengths)

        # Create initial decoder input (start with SOS tokens for each sentence)
        decoder_input = torch.LongTensor([[SOS_token for _ in range(batch_size)]]).to(device)
        encoder_hidden = encoder_hidden.view(self.layer_num, 2, batch_size, self.hidden_size)
        decoder_hidden = encoder_hidden[:, 0] + encoder_hidden[:, 1]

        loss = 0

        for t in range(max_target_len):
            decoder_output, decoder_hidden = self.decode(
                decoder_input, decoder_hidden, encoder_outputs
            )
            # No Teacher forcing
            _, topi = decoder_output.topk(1)

            decoder_input = torch.LongTensor([[topi[i][0] for i in range(batch_size)]]).to(device)
            # Calculate and accumulate loss
            mask_loss, nTotal = maskNLLLoss(decoder_output, target_variable[t], mask[t])
            loss += mask_loss
        return loss

    def forward_teacher_forcing(self, input_variable, input_lengths, target_variable, mask, max_target_len):
        assert False, "TODO"


def maskNLLLoss(inp, target, mask):
    nTotal = mask.sum()
    crossEntropy = -torch.log(torch.gather(inp, 1, target.view(-1, 1)).squeeze(1))
    loss = crossEntropy.masked_select(mask).mean()
    loss = loss.to(device)
    return loss, nTotal.item()


def train(input_variable, input_lengths, target_variable, mask, max_target_len, model, optimizer, clip, no_teacher_forcing):

    # Zero gradients
    optimizer.zero_grad()

    if no_teacher_forcing:
        loss = model.forward_no_teacher_forcing(input_variable, input_lengths, target_variable, mask, max_target_len)
    else:
        loss = model.forward_teacher_forcing(input_variable, input_lengths, target_variable, mask, max_target_len)

    # Perform backpropatation
    loss.backward()

    # Clip gradients: gradients are modified in place
    _ = nn.utils.clip_grad_norm_(model.parameters(), clip)

    # Adjust model weights
    optimizer.step()

    return float(loss) / max_target_len


def trainIters(voc, pairs, model, optimizer, n_iteration, batch_size, print_every, clip, no_teacher_forcing):

    # Load batches for each iteration
    training_batches = [batch2TrainData(voc, [random.choice(pairs) for _ in range(batch_size)])
                      for _ in range(n_iteration)]

    # Initializations
    print('Initializing ...')
    start_iteration = 1
    print_loss = 0
    start_time = time.time()

    # Training loop
    print("Training...")
    for iteration in range(start_iteration, n_iteration + 1):
        training_batch = training_batches[iteration - 1]
        # Extract fields from batch
        input_variable, lengths, target_variable, mask, max_target_len = training_batch

        # Run a training iteration with batch
        avg_loss = train(input_variable, lengths, target_variable, mask, max_target_len, model, optimizer, clip, no_teacher_forcing)
        print_loss += avg_loss

        # Print progress
        if iteration % print_every == 0:
            print_loss_avg = print_loss / print_every
            print("Iteration: {}; time: {:.1f}, Percent complete: {:.1f}%; Average loss: {:.4f}".format(iteration, time.time() - start_time, iteration / n_iteration * 100, print_loss_avg))
            print_loss = 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['gru', 'transformer', 'bert'])
    parser.add_argument('--no_teacher_forcing', action='store_true')
    args = parser.parse_args()

    create_formatted_data_file()
    voc, pairs = loadPrepareData()

    # Configurations
    hidden_size = 500
    layer_num = 2
    dropout = 0.1
    batch_size = 64
    clip = 50.0
    learning_rate = 0.0001
    n_iteration = 4000
    print_every = 1

    print('Building encoder and decoder ...')
    if args.model == 'gru':
        model = GRUSeq2SeqWithAttention(voc.num_words, hidden_size, layer_num, dropout)
    elif args.model == 'transformer':
        assert False, 'need impementation'
    else:
        assert args.model == 'bert'
        assert False, 'need implementation'

    # Initialize word embeddings
    model = model.to(device)
    model.train()
    print('Models built and ready to go!')

    # Initialize optimizers
    print('Building optimizers ...')
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)

    # If you have cuda, configure cuda to call
    for state in optimizer.state.values():
        for k, v in state.items():
            if isinstance(v, torch.Tensor):
                state[k] = v.cuda()

    # Run training iterations
    print("Starting Training!")
    trainIters(voc, pairs, model, optimizer, n_iteration, batch_size, print_every, clip, args.no_teacher_forcing)


if __name__ == "__main__":
    main()