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
import argparse
import time
from batzzang.lazy_modules import LazyGRU, LazyEmbedding, LazyLinear, LazyAttention, LazyTransformerEncoder, LazyTransformerDecoder, LazyBert
from batzzang.monad import ForEachState, While, If, Do
from load_and_trim_data import EOS_token, PAD_token, SOS_token, MAX_LENGTH, loadPrepareData, trimRareWords
from create_formatted_data_file import create_formatted_data_file


USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda" if USE_CUDA else "cpu")


def indexesFromSentence(voc, sentence):
    return [voc.word2index[word] for word in sentence.split(' ')] + [EOS_token]


# Returns padded input sequence tensor and lengths
def makeVar(l, voc, tokenizer=None):
    if tokenizer:
        indexes_batch = tokenizer(l, return_tensors='pt', padding=True).to("cuda")
    else:
        indexes_batch = [torch.LongTensor(indexesFromSentence(voc, sentence)).to(device) for sentence in l]
    return indexes_batch


# Returns all items for a given batch of pairs
def batch2TrainData(voc, pair_batch, model):
    tokenizer = model.encoder.tokenizer if hasattr(model, "encoder") and hasattr(model.encoder, "tokenizer") else None
    pair_batch.sort(key=lambda x: len(x[0].split(" ")), reverse=True)
    input_batch, output_batch = [], []
    for pair in pair_batch:
        input_batch.append(pair[0])
        output_batch.append(pair[1])
    inp = makeVar(input_batch, voc, tokenizer=tokenizer)
    output = makeVar(output_batch, voc)
    return inp, output


class State:
    def __init__(self, input_seq, target_seq):
        self.input_seq = input_seq
        self.target_seq = target_seq
        self.decoder_input_token = torch.LongTensor([SOS_token]).to(device)
        self.decoding_cnt = 0
        self.loss = 0

class StateBert(State):
    def __init__(self, input_seq, target_seq):
        super(StateBert, self).__init__(input_seq['input_ids'], target_seq)
        self.input_length = (input_seq['attention_mask'] != 0).sum().item()


class StateTeacherForcing:
    def __init__(self, input_seq, target_seq):
        self.input_seq = input_seq
        self.target_seq = target_seq
        self.decoder_input = torch.cat((torch.LongTensor([SOS_token]).to(device), target_seq[:-1]), dim=0)

        self.loss = 0


class StateTeacherForcingBert(StateTeacherForcing):
    def __init__(self, input_seq, target_seq):
        super(StateTeacherForcingBert, self).__init__(input_seq['input_ids'], target_seq)
        self.input_length = (input_seq['attention_mask'] != 0).sum().item()



class GRUSeq2SeqWithAttentionWithBatzzang(nn.Module):
    def __init__(self, vocab_num, hidden_size, layer_num, dropout):
        super(GRUSeq2SeqWithAttentionWithBatzzang, self).__init__()
        self.embedding = LazyEmbedding(vocab_num, hidden_size, dropout)
        self.encoder_gru = LazyGRU(hidden_size, hidden_size, layer_num,
                                   dropout=(0 if layer_num == 1 else dropout), bidirection=True)
        self.decoder_gru = LazyGRU(hidden_size, hidden_size, layer_num,
                                   dropout=(0 if layer_num == 1 else dropout), bidirection=False)
        self.layer_num = layer_num
        self.att = LazyAttention(hidden_size, 'dot')
        self.out = LazyLinear(hidden_size, vocab_num)
        self.hidden_size = hidden_size
        self.loss = nn.CrossEntropyLoss()

    def forward_no_teacher_forcing(self, input_seq_batch, target_seq_batch):
        states = [
            State(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def embed(state: State, _):
            return self.embedding.forward_later(state.input_seq)

        def encode(state: State, previous_output):
            embedded_tensor = previous_output.result
            return self.encoder_gru.forward_later(embedded_tensor)

        def is_not_done(state: State):
            return state.decoding_cnt < len(state.target_seq)

        def embed_decoder_input(state: State, previous_output):
            if state.decoding_cnt == 0:
                encoder_promise = previous_output
                encoder_output, encoder_hidden = encoder_promise.result
                encoder_output = encoder_output[:, :self.hidden_size] + encoder_output[:, self.hidden_size:]
                decoder_hidden = encoder_hidden[:, 0] + encoder_hidden[:, 1]
                decoder_input = state.decoder_input_token
            else:
                encoder_output, decoder_hidden = previous_output
                decoder_input = state.decoder_input_token

            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_hidden, decoder_input_embedded_promise

        def decode_gru(state: State, previous_output):
            encoder_output, decoder_hidden, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return encoder_output, self.decoder_gru.forward_later(decoder_input_embedded, decoder_hidden)

        def attention(state: State, previous_output):
            encoder_output, decoder_hidden_promise = previous_output
            decoder_output, decoder_hidden = decoder_hidden_promise.result
            return encoder_output, decoder_hidden, self.att.forward_later(decoder_output, encoder_output)

        def out(state: State, previous_output):
            encoder_output, decoder_hidden, attention_promise = previous_output
            attention_output, _ = attention_promise.result
            output_promise = self.out.forward_later(attention_output)
            return encoder_output, decoder_hidden, output_promise

        def calc_loss(state: State, previous_output):
            encoder_output, decoder_hidden, output_promise = previous_output
            output = F.softmax(output_promise.result.squeeze(0), dim=-1)
            target_idx = state.target_seq[state.decoding_cnt]
            state.loss += -torch.log(output[target_idx])

            state.decoding_cnt += 1
            state.decoder_input_token = torch.argmax(output).unsqueeze(0)
            return encoder_output, decoder_hidden

        result_states = ForEachState(states).apply(
            Do(embed)
                .Then(encode)
        ).apply(
            While.Any(is_not_done,
                If(is_not_done)
                    .Then(embed_decoder_input)
                    .Then(decode_gru)
                    .Then(attention)
                    .Then(out)
                    .Then(calc_loss)
            )
        ).states

        return sum([state.loss / state.decoding_cnt for state in result_states])

    def forward_teacher_forcing(self, input_seq_batch, target_seq_batch):
        states = [
            StateTeacherForcing(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def embed(state: StateTeacherForcing, _):
            return self.embedding.forward_later(state.input_seq)

        def encode(state: StateTeacherForcing, previous_output):
            embedded_tensor = previous_output.result
            return self.encoder_gru.forward_later(embedded_tensor)

        def embed_decoder_input(state: StateTeacherForcing, previous_output):
            encoder_promise = previous_output
            encoder_output, encoder_hidden = encoder_promise.result

            encoder_output = encoder_output[:, :self.hidden_size] + encoder_output[:, self.hidden_size:]
            decoder_hidden = encoder_hidden[:, 0] + encoder_hidden[:, 1]
            decoder_input = state.decoder_input

            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_hidden, decoder_input_embedded_promise

        def decode_gru(state: StateTeacherForcing, previous_output):
            encoder_output, decoder_hidden, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return encoder_output, self.decoder_gru.forward_later(decoder_input_embedded, decoder_hidden)

        def attention(state: StateTeacherForcing, previous_output):
            encoder_output, decoder_hidden_promise = previous_output
            decoder_output, decoder_hidden = decoder_hidden_promise.result
            return self.att.forward_later(decoder_output, encoder_output)

        def out(state: StateTeacherForcing, previous_output):
            attention_promise = previous_output
            attention_output, _ = attention_promise.result
            output_promise = self.out.forward_later(attention_output)
            return output_promise

        def calc_loss(state: StateTeacherForcing, previous_output):
            output_promise = previous_output
            state.loss = self.loss(output_promise.result, state.target_seq)
            return None

        result_states = ForEachState(states).apply(
            Do(embed)
                .Then(encode)
                .Then(embed_decoder_input)
                .Then(decode_gru)
                .Then(attention)
                .Then(out)
                .Then(calc_loss)
        ).states


        return sum([state.loss for state in result_states])


class TransformerSeq2SeqWithBatzzang(nn.Module):
    def __init__(self, vocab_num, hidden_size, layer_num, dropout):
        super(TransformerSeq2SeqWithBatzzang, self).__init__()
        self.embedding = LazyEmbedding(vocab_num, hidden_size, dropout)
        self.encoder_transformer = LazyTransformerEncoder(hidden_size, nhead=8, dim_feedforward=1024, layer_num=4)
        self.decoder_transformer = LazyTransformerDecoder(hidden_size, nhead=8, dim_feedforward=1024, layer_num=4)
        self.layer_num = layer_num
        self.out = LazyLinear(hidden_size, vocab_num)
        self.hidden_size = hidden_size
        self.loss = nn.CrossEntropyLoss()

    def forward_no_teacher_forcing(self, input_seq_batch, target_seq_batch):
        states = [
            State(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def embed(state: State, _):
            return self.embedding.forward_later(state.input_seq)

        def encode(state: State, previous_output):
            embedded_tensor = previous_output.result
            return self.encoder_transformer.forward_later(embedded_tensor)

        def is_not_done(state: State):
            return state.decoding_cnt < len(state.target_seq)

        def embed_decoder_input(state: State, previous_output):
            if state.decoding_cnt == 0:
                encoder_promise = previous_output
                encoder_output = encoder_promise.result
                decoder_input = state.decoder_input_token
            else:
                encoder_output = previous_output
                decoder_input = state.decoder_input_token

            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_input_embedded_promise

        def decode_transformer(state: State, previous_output):
            encoder_output, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return encoder_output, self.decoder_transformer.forward_later(decoder_input_embedded, encoder_output)

        def out(state: State, previous_output):
            encoder_output, decoder_output_promise = previous_output
            decoder_output = decoder_output_promise.result
            output_promise = self.out.forward_later(decoder_output)
            return encoder_output, output_promise

        def calc_loss(state: State, previous_output):
            encoder_output, output_promise, = previous_output
            output = F.softmax(output_promise.result.squeeze(0), dim=-1)
            target_idx = state.target_seq[state.decoding_cnt]
            state.loss += - torch.log(output[target_idx])

            state.decoding_cnt += 1
            state.decoder_input_token = torch.argmax(output).unsqueeze(0)
            return encoder_output

        result_states = ForEachState(states).apply(
            Do(embed)
            .Then(encode)
        ).apply(
            While.Any(is_not_done,
                    If(is_not_done)
                      .Then(embed_decoder_input)
                      .Then(decode_transformer)
                      .Then(out)
                      .Then(calc_loss)
            )
        ).states

        return sum([state.loss / state.decoding_cnt for state in result_states])

    def forward_teacher_forcing(self, input_seq_batch, target_seq_batch):
        states = [
            StateTeacherForcing(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def embed(state:StateTeacherForcing, _):
            return self.embedding.forward_later(state.input_seq)

        def encode(state: StateTeacherForcing, previous_output):
            embedded_tensor = previous_output.result
            return self.encoder_transformer.forward_later(embedded_tensor)

        def embed_decoder_input(state: StateTeacherForcing, previous_output):
            encoder_promise = previous_output
            encoder_output = encoder_promise.result
            decoder_input = state.decoder_input
            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_input_embedded_promise

        def decode_transformer(state: StateTeacherForcing, previous_output):
            encoder_output, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return self.decoder_transformer.forward_later(decoder_input_embedded, encoder_output)

        def out(state: StateTeacherForcing, previous_output):
            decoder_promise = previous_output
            decoder_output = decoder_promise.result
            output_promise = self.out.forward_later(decoder_output)
            return output_promise

        def calc_loss(state: StateTeacherForcing, previous_output):
            output_promise = previous_output
            state.loss = self.loss(output_promise.result, state.target_seq)
            return None

        result_states = ForEachState(states).apply(
            Do(embed)
            .Then(encode)
            .Then(embed_decoder_input)
            .Then(decode_transformer)
            .Then(out)
            .Then(calc_loss)
        ).states

        return sum([state.loss for state in result_states])


class BertSeq2SeqWithBatzzang(nn.Module):
    def __init__(self, vocab_num, _, layer_num, dropout):
        super(BertSeq2SeqWithBatzzang, self).__init__()
        hidden_size = 768
        self.embedding = LazyEmbedding(vocab_num, hidden_size, dropout)
        self.encoder = LazyBert()
        self.decoder_transformer = LazyTransformerDecoder(hidden_size, 2, 1024, 2)
        self.layer_num = layer_num
        self.out = LazyLinear(hidden_size, vocab_num)
        self.hidden_size = hidden_size
        self.loss = nn.CrossEntropyLoss()

    def forward_no_teacher_forcing(self, input_seq_batch, target_seq_batch):
        input_seq_batch = [{"input_ids": input_seq_batch.data['input_ids'][idx],
                            "attention_mask": input_seq_batch.data['attention_mask'][idx]}
                           for idx in range(input_seq_batch.data['attention_mask'].size(0))]

        states = [
            StateBert(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def encode(state: StateBert, _):
            return self.encoder.forward_later(state.input_seq)

        def is_not_done(state: StateBert):
            return state.decoding_cnt < len(state.target_seq)

        def embed_decoder_input(state: StateBert, previous_output):
            if state.decoding_cnt == 0:
                encoder_promise = previous_output
                encoder_output = encoder_promise.result
                decoder_input = state.decoder_input_token
            else:
                encoder_output = previous_output
                decoder_input = state.decoder_input_token

            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_input_embedded_promise

        def decode_transformer(state: StateBert, previous_output):
            encoder_output, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return encoder_output, self.decoder_transformer.forward_later(decoder_input_embedded, encoder_output)

        def out(state: StateBert, previous_output):
            encoder_output, decoder_output_promise = previous_output
            decoder_output = decoder_output_promise.result
            output_promise = self.out.forward_later(decoder_output)
            return encoder_output, output_promise

        def calc_loss(state: StateBert, previous_output):
            encoder_output, output_promise, = previous_output
            output = F.softmax(output_promise.result.squeeze(0), dim=-1)
            target_idx = state.target_seq[state.decoding_cnt]
            state.loss += - torch.log(output[target_idx])

            state.decoding_cnt += 1
            state.decoder_input_token = torch.argmax(output).unsqueeze(0)
            return encoder_output

        result_states = ForEachState(states).apply(
            Do(encode)
        ).apply(
            While.Any(is_not_done,
                    If(is_not_done)
                      .Then(embed_decoder_input)
                      .Then(decode_transformer)
                      .Then(out)
                      .Then(calc_loss)
            )
        ).states

        return sum([state.loss / state.decoding_cnt for state in result_states])

    def forward_teacher_forcing(self, input_seq_batch, target_seq_batch):
        input_seq_batch = [{"input_ids": input_seq_batch.data['input_ids'][idx],
                            "attention_mask": input_seq_batch.data['attention_mask'][idx]}
                           for idx in range(input_seq_batch.data['attention_mask'].size(0))]
        states = [
            StateTeacherForcingBert(input_seq, target_seq)
            for input_seq, target_seq in zip(input_seq_batch, target_seq_batch)
        ]

        def encode(state: StateTeacherForcingBert, _):
            return self.encoder.forward_later(state.input_seq)

        def embed_decoder_input(state: StateTeacherForcingBert, previous_output):
            encoder_promise = previous_output
            encoder_output = encoder_promise.result
            decoder_input = state.decoder_input
            decoder_input_embedded_promise = self.embedding.forward_later(decoder_input)
            return encoder_output, decoder_input_embedded_promise

        def decode_transformer(state: StateTeacherForcingBert, previous_output):
            encoder_output, decoder_input_embedded_promise = previous_output
            decoder_input_embedded = decoder_input_embedded_promise.result
            return self.decoder_transformer.forward_later(decoder_input_embedded, encoder_output)

        def out(state: StateTeacherForcingBert, previous_output):
            decoder_promise = previous_output
            decoder_output = decoder_promise.result
            output_promise = self.out.forward_later(decoder_output)
            return output_promise

        def calc_loss(state: StateTeacherForcingBert, previous_output):
            output_promise = previous_output
            state.loss = self.loss(output_promise.result, state.target_seq)
            return None

        result_states = ForEachState(states).apply(
            Do(encode)
            .Then(embed_decoder_input)
            .Then(decode_transformer)
            .Then(out)
            .Then(calc_loss)
        ).states

        return sum([state.loss for state in result_states])


def train(input_seq_batch, target_seq_batch, model, optimizer, clip, no_teacher_forcing):

    # Zero gradients
    optimizer.zero_grad()

    if no_teacher_forcing:
        loss = model.forward_no_teacher_forcing(input_seq_batch, target_seq_batch)
    else:
        loss = model.forward_teacher_forcing(input_seq_batch, target_seq_batch)

    # Perform backpropatation
    loss.backward()

    # Clip gradients: gradients are modified in place
    _ = nn.utils.clip_grad_norm_(model.parameters(), clip)

    # Adjust model weights
    optimizer.step()

    return float(loss) / len(input_seq_batch)


def trainIters(voc, pairs, model, optimizer, n_iteration, batch_size, print_every, clip, no_teacher_forcing):

    # Load batches for each iteration
    training_batches = [batch2TrainData(voc, [random.choice(pairs) for _ in range(batch_size)], model)
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
        input_variable, target_variable = training_batch

        # Run a training iteration with batch
        avg_loss = train(input_variable, target_variable, model, optimizer, clip, no_teacher_forcing)
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
    hidden_size = 512
    layer_num = 2
    dropout = 0.1
    batch_size = 64
    clip = 50.0
    learning_rate = 0.0001
    n_iteration = 4000
    print_every = 1

    print('Building model ...')
    if args.model == 'gru':
        model = GRUSeq2SeqWithAttentionWithBatzzang(voc.num_words, hidden_size, layer_num, dropout)
    elif args.model == 'transformer':
        model = TransformerSeq2SeqWithBatzzang(voc.num_words, hidden_size, layer_num, dropout)
    else:
        assert args.model == 'bert'
        model = BertSeq2SeqWithBatzzang(voc.num_words, hidden_size, layer_num, dropout)

    # Use appropriate device
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
