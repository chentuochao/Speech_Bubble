import math
from collections import OrderedDict
from typing import Optional

from torch import Tensor
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchmetrics.functional import(
    scale_invariant_signal_noise_ratio as si_snr,
    signal_noise_ratio as snr,
    signal_distortion_ratio as sdr,
    scale_invariant_signal_distortion_ratio as si_sdr)

from speechbrain.lobes.models.transformer.Transformer import PositionalEncoding
from asteroid.losses.sdr import SingleSrcNegSDR

def mod_pad(x, chunk_size, pad):
    # Mod pad the input to perform integer number of
    # inferences
    mod = 0
    if (x.shape[-1] % chunk_size) != 0:
        mod = chunk_size - (x.shape[-1] % chunk_size)

    x = F.pad(x, (0, mod))
    x = F.pad(x, pad)

    return x, mod


def normalize_input(data):
    """
    Normalizes the input to have mean 0 std 1 for each input
    Inputs:
        data - torch.tensor of size batch x n_mics x n_samples
    """
    data = (data * 2**15).round() / 2**15
    ref = data.mean(1)  # Average across the n microphones
    means = ref.mean(1).unsqueeze(1).unsqueeze(2)
    stds = ref.std(1).unsqueeze(1).unsqueeze(2)
    data = (data - means) / stds

    return data, means, stds

def unnormalize_input(data, means, stds):
    """
    Unnormalizes the step done in the previous function
    """
    data = (data * stds + means)
    return data


class LayerNormPermuted(nn.LayerNorm):
    def __init__(self, *args, **kwargs):
        super(LayerNormPermuted, self).__init__(*args, **kwargs)

    def forward(self, x):
        """
        Args:
            x: [B, C, T]
        """
        x = x.permute(0, 2, 1) # [B, T, C]
        x = super().forward(x)
        x = x.permute(0, 2, 1) # [B, C, T]
        return x

class DepthwiseSeparableConv(nn.Module):
    """
    Depthwise separable convolutions
    """
    def __init__(self, in_channels, out_channels, kernel_size, stride,
                 padding, dilation):
        super(DepthwiseSeparableConv, self).__init__()

        self.layers = nn.Sequential(
            nn.Conv1d(in_channels, in_channels, kernel_size, stride,
                      padding, groups=in_channels, dilation=dilation),
            LayerNormPermuted(in_channels),
            nn.ReLU(),
            nn.Conv1d(in_channels, out_channels, kernel_size=1, stride=1,
                      padding=0),
            LayerNormPermuted(out_channels),
            nn.ReLU(),
        )

    def forward(self, x):
        return self.layers(x)

class DilatedCausalConvEncoder(nn.Module):
    """
    A dilated causal convolution based encoder for encoding
    time domain audio input into latent space.
    """
    def __init__(self, channels, num_layers, kernel_size=3):
        super(DilatedCausalConvEncoder, self).__init__()
        self.channels = channels
        self.num_layers = num_layers
        self.kernel_size = kernel_size

        # Compute buffer lengths for each layer
        # buf_length[i] = (kernel_size - 1) * dilation[i]
        self.buf_lengths = [(kernel_size - 1) * 2**i
                            for i in range(num_layers)]

        # Compute buffer start indices for each layer
        self.buf_indices = [0]
        for i in range(num_layers - 1):
            self.buf_indices.append(
                self.buf_indices[-1] + self.buf_lengths[i])

        # Dilated causal conv layers aggregate previous context to obtain
        # contexful encoded input.
        _dcc_layers = OrderedDict()
        for i in range(num_layers):
            dcc_layer = DepthwiseSeparableConv(
                channels, channels, kernel_size=3, stride=1,
                padding=0, dilation=2**i)
            _dcc_layers.update({'dcc_%d' % i: dcc_layer})
        self.dcc_layers = nn.Sequential(_dcc_layers)

    def init_ctx_buf(self, batch_size, device):
        """
        Returns an initialized context buffer for a given batch size.
        """
        return torch.zeros(
            (batch_size, self.channels,
                 (self.kernel_size - 1) * (2**self.num_layers - 1)),
            device=device)

    def forward(self, x, ctx_buf):
        """
        Encodes input audio `x` into latent space, and aggregates
        contextual information in `ctx_buf`. Also generates new context
        buffer with updated context.
        Args:
            x: [B, in_channels, T]
                Input multi-channel audio.
            ctx_buf: {[B, channels, self.buf_length[0]], ...}
                A list of tensors holding context for each dilation
                causal conv layer. (len(ctx_buf) == self.num_layers)
        Returns:
            ctx_buf: {[B, channels, self.buf_length[0]], ...}
                Updated context buffer with output as the
                last element.
        """
        T = x.shape[-1] # Sequence length

        for i in range(self.num_layers):
            buf_start_idx = self.buf_indices[i]
            buf_end_idx = self.buf_indices[i] + self.buf_lengths[i]

            # DCC input: concatenation of current output and context
            dcc_in = torch.cat(
                (ctx_buf[..., buf_start_idx:buf_end_idx], x), dim=-1)

            # Push current output to the context buffer
            ctx_buf[..., buf_start_idx:buf_end_idx] = \
                dcc_in[..., -self.buf_lengths[i]:]

            # Residual connection
            x = x + self.dcc_layers[i](dcc_in)

        return x, ctx_buf

class CausalTransformerDecoderLayer(torch.nn.TransformerDecoderLayer):
    """
    Adapted from:
    "https://github.com/alexmt-scale/causal-transformer-decoder/blob/"
    "0caf6ad71c46488f76d89845b0123d2550ef792f/"
    "causal_transformer_decoder/model.py#L77"
    """
    def forward(
        self,
        tgt: Tensor,
        memory: Optional[Tensor] = None,
        chunk_size: int = 1
    ) -> Tensor:
        tgt_last_tok = tgt[:, -chunk_size:, :]

        # self attention part
        tmp_tgt, sa_map = self.self_attn(
            tgt_last_tok,
            tgt,
            tgt,
            attn_mask=None,  # not needed because we only care about the last token
            key_padding_mask=None,
        )
        tgt_last_tok = tgt_last_tok + self.dropout1(tmp_tgt)
        tgt_last_tok = self.norm1(tgt_last_tok)

        # encoder-decoder attention
        if memory is not None:
            tmp_tgt, ca_map = self.multihead_attn(
                tgt_last_tok,
                memory,
                memory,
                attn_mask=None, # Attend to the entire chunk
                key_padding_mask=None,
            )
            tgt_last_tok = tgt_last_tok + self.dropout2(tmp_tgt)
            tgt_last_tok = self.norm2(tgt_last_tok)

        # final feed-forward network
        tmp_tgt = self.linear2(
            self.dropout(self.activation(self.linear1(tgt_last_tok)))
        )
        tgt_last_tok = tgt_last_tok + self.dropout3(tmp_tgt)
        tgt_last_tok = self.norm3(tgt_last_tok)
        return tgt_last_tok, sa_map, ca_map

class CausalTransformerDecoder(nn.Module):
    """
    A casual transformer decoder which decodes input vectors using
    precisely `ctx_len` past vectors in the sequence, and using no future
    vectors at all.
    """
    def __init__(self, model_dim, ctx_len, chunk_size, num_layers,
                 nhead, use_pos_enc, ff_dim):
        super(CausalTransformerDecoder, self).__init__()
        self.num_layers = num_layers
        self.model_dim = model_dim
        self.ctx_len = ctx_len
        self.chunk_size = chunk_size
        self.nhead = nhead
        self.use_pos_enc = use_pos_enc
        self.unfold = nn.Unfold(kernel_size=(ctx_len + chunk_size, 1), stride=chunk_size)
        self.pos_enc = PositionalEncoding(model_dim, max_len=200)
        self.tf_dec_layers = nn.ModuleList([CausalTransformerDecoderLayer(
            d_model=model_dim, nhead=nhead, dim_feedforward=ff_dim,
            batch_first=True) for _ in range(num_layers)])

    def init_ctx_buf(self, batch_size, device):
        return torch.zeros(
            (batch_size, self.num_layers + 1, self.ctx_len, self.model_dim),
            device=device)

    def _causal_unfold(self, x):
        """
        Unfolds the sequence into a batch of sequences
        prepended with `ctx_len` previous values.

        Args:
            x: [B, ctx_len + L, C], L is total length of signal
            ctx_len: int
        Returns:
            [B * L, ctx_len + 1, C]
        """
        B, T, C = x.shape
        x = x.permute(0, 2, 1) # [B, C, ctx_len + L]
        ### num_chunk = L//chunk_size
        x = self.unfold(x.unsqueeze(-1)) 
        # x - [B, C * (ctx_len + chunk_size), num_chunk]
        # print("x after unfold = ", x.shape)
        x = x.permute(0, 2, 1)
        # x - [B, num_chunk, C * (ctx_len + chunk_size)]
        x = x.reshape(B, -1, C, self.ctx_len + self.chunk_size)
        # x - [B, num_chunk, C, (ctx_len + chunk_size)]
        x = x.reshape(-1, C, self.ctx_len + self.chunk_size)
        # x - [B*num_chunk, C, (ctx_len + chunk_size)]
        x = x.permute(0, 2, 1)
        # x - [B*num_chunk,  (ctx_len + chunk_size), C]
        return x

    def forward(self, tgt, mem, ctx_buf, probe=False):
        """
        Args:
            x: [B, model_dim, T]
            ctx_buf: [B, num_layers, model_dim, ctx_len]
        """
        mem, _ = mod_pad(mem, self.chunk_size, (0, 0))
        tgt, mod = mod_pad(tgt, self.chunk_size, (0, 0))

        # Input sequence length
        B, C, T = tgt.shape

        tgt = tgt.permute(0, 2, 1)
        mem = mem.permute(0, 2, 1)

        # Prepend mem with the context
        # print("mem: ", mem.shape)
        mem = torch.cat((ctx_buf[:, 0, :, :], mem), dim=1)
        ctx_buf[:, 0, :, :] = mem[:, -self.ctx_len:, :]


        # print("mem: ",mem.shape)
        mem_ctx = self._causal_unfold(mem)
        # print("mem_ctx: ",mem_ctx.shape)
        if self.use_pos_enc:
            mem_ctx = mem_ctx + self.pos_enc(mem_ctx)

        # Attention chunk size: required to ensure the model
        # wouldn't trigger an out-of-memory error when working
        # on long sequences.
        K = 1000

        for i, tf_dec_layer in enumerate(self.tf_dec_layers):
            # Update the tgt with context
            tgt = torch.cat((ctx_buf[:, i + 1, :, :], tgt), dim=1)
            ctx_buf[:, i + 1, :, :] = tgt[:, -self.ctx_len:, :]

            # Compute encoded output
            tgt_ctx = self._causal_unfold(tgt)
            if self.use_pos_enc and i == 0:
                tgt_ctx = tgt_ctx + self.pos_enc(tgt_ctx)
            tgt = torch.zeros_like(tgt_ctx)[:, -self.chunk_size:, :]
            for i in range(int(math.ceil(tgt.shape[0] / K))):
                tgt[i*K:(i+1)*K], _sa_map, _ca_map = tf_dec_layer(
                    tgt_ctx[i*K:(i+1)*K], mem_ctx[i*K:(i+1)*K],
                    self.chunk_size)
            tgt = tgt.reshape(B, T, C)

        tgt = tgt.permute(0, 2, 1)
        if mod != 0:
            tgt = tgt[..., :-mod]

        return tgt, ctx_buf

class MaskNet(nn.Module):
    def __init__(self, enc_dim, num_enc_layers, dec_dim, dec_buf_len,
                 dec_chunk_size, num_dec_layers, use_pos_enc, skip_connection, proj):
        super(MaskNet, self).__init__()
        self.skip_connection = skip_connection
        self.proj = proj

        # Encoder based on dilated causal convolutions.
        self.encoder = DilatedCausalConvEncoder(channels=enc_dim,
                                                num_layers=num_enc_layers)

        # Project between encoder and decoder dimensions
        self.proj_e2d_e = nn.Sequential(
            nn.Conv1d(enc_dim, dec_dim, kernel_size=1, stride=1, padding=0,
                      groups=dec_dim),
            nn.ReLU())
        self.proj_d2e = nn.Sequential(
            nn.Conv1d(dec_dim, enc_dim, kernel_size=1, stride=1, padding=0,
                      groups=dec_dim),
            nn.ReLU())

        # Transformer decoder that operates on chunks of size
        # buffer size.
        self.decoder = CausalTransformerDecoder(
            model_dim=dec_dim, ctx_len=dec_buf_len, chunk_size=dec_chunk_size,
            num_layers=num_dec_layers, nhead=8, use_pos_enc=use_pos_enc,
            ff_dim=2 * dec_dim)

    def forward(self, x, enc_buf, dec_buf):
        """
        Generates a mask based on encoded input `e` and the one-hot
        label `label`.

        Args:
            x: [B, C, T]
                Input audio sequence
            l: [B, C]
                Label embedding
            ctx_buf: {[B, C, <receptive field of the layer>], ...}
                List of context buffers maintained by DCC encoder
        """
        # Enocder the label integrated input
        x, enc_buf = self.encoder(x, enc_buf)

        # Label integration
        # l = l.unsqueeze(2) * e

        # Project to `dec_dim` dimensions
        if self.proj:
            e = self.proj_e2d_e(x)
            # Cross-attention to predict the mask
            m, dec_buf = self.decoder(e, e, dec_buf)
        else:
            # Cross-attention to predict the mask
            m, dec_buf = self.decoder(x, x, dec_buf)

        # Project mask to encoder dimensions
        if self.proj:
            m = self.proj_d2e(m)

        # Final mask after residual connection
        if self.skip_connection:
            m = x + m

        return m, enc_buf, dec_buf

class Net(nn.Module):
    def __init__(self, n_mics = 1, L=8,
                 enc_dim=512, num_enc_layers=10,
                 dec_dim=256, dec_buf_len=100, num_dec_layers=2,
                 dec_chunk_size=72, out_buf_len=2, r=1.,
                 use_pos_enc=True, skip_connection=True, proj=True, lookahead=True, fair_compare = False, loss_type = "sisdr"):
        super(Net, self).__init__()
        self.L = L
        self.out_buf_len = out_buf_len
        self.enc_dim = enc_dim
        self.lookahead = lookahead
        self.r = r

        # Input conv to convert input audio to a latent representation
        kernel_size = 7 * L if lookahead else L

        self.in_conv = nn.Sequential(
            nn.Conv1d(in_channels=n_mics,
                    out_channels=enc_dim, kernel_size=kernel_size, stride=L,
                    padding=0, bias=False),
            nn.ReLU())

        # Mask generator
        self.mask_gen = MaskNet(
            enc_dim=enc_dim, num_enc_layers=num_enc_layers,
            dec_dim=dec_dim, dec_buf_len=dec_buf_len,
            dec_chunk_size=dec_chunk_size, num_dec_layers=num_dec_layers,
            use_pos_enc=use_pos_enc, skip_connection=skip_connection, proj=proj)

        # Output conv layer
        self.out_conv = nn.Sequential(
            nn.ConvTranspose1d(
                in_channels=enc_dim, out_channels=1,
                kernel_size=(out_buf_len + 1) * L,
                stride=L,
                padding=out_buf_len * L, bias=False),
            )
    

    def init_buffers(self, batch_size, device):
        enc_buf = self.mask_gen.encoder.init_ctx_buf(batch_size, device)
        dec_buf = self.mask_gen.decoder.init_ctx_buf(batch_size, device)
        out_buf = torch.zeros(batch_size, self.enc_dim, self.out_buf_len,
                              device=device)
        return enc_buf, dec_buf, out_buf


    def forward(self, inputs, input_state = None, pad=True):
        x = inputs['mixture']

        if input_state is None:
            input_state = self.init_buffers(x.shape[0], x.device)

        x, next_state = self.predict(x, input_state, pad)

        return {'output': x, 'next_state': next_state}

    def predict(self, x,  input_state, pad=True):
        """
        Extracts the audio corresponding to the `label` in the given
        `mixture`. Generates `chunk_size` samples per iteration.

        Args:
            mixed: [B, n_mics, T]
                input audio mixture
            label: [B, num_labels]
                one hot label
        Returns:
            out: [B, n_spk, T]
                extracted audio with sounds corresponding to the `label`
        """
        mod = 0
        if pad:
            pad_size = (0, 6*self.L) if self.lookahead else (0, 0)
            x, mod = mod_pad(x, chunk_size=self.L, pad=pad_size)

    
        enc_buf, dec_buf, out_buf = input_state

        # Generate latent space representation of the input
        x = self.in_conv(x)

        # Generate label embedding
        # l = self.label_embedding(label) # [B, label_len] --> [B, channels]

        # Generate mask corresponding to the label
        m, enc_buf, dec_buf = self.mask_gen(x, enc_buf, dec_buf)

        # Apply mask and decode
        x = x * m
        x = torch.cat((out_buf, x), dim=-1)
        out_buf = x[..., -self.out_buf_len:]
        x = self.out_conv(x)

        # Remove mod padding, if present.
        if mod != 0:
            x = x[:, :, :-mod]

        return x, None

    def forward_track(self, x, init_enc_buf=None, init_dec_buf=None,
                init_out_buf=None, pad=True):

        mod = 0
        if pad:
            pad_size = (0, 0)
            x, mod = mod_pad(x, chunk_size=self.L, pad=pad_size)

        if init_enc_buf is None or init_dec_buf is None or init_out_buf is None:
            assert init_enc_buf is None and \
                   init_dec_buf is None and \
                   init_out_buf is None, \
                "Both buffers have to initialized, or " \
                "both of them have to be None."
            enc_buf, dec_buf, out_buf = self.init_buffers(
                x.shape[0], x.device)
        else:
            enc_buf, dec_buf, out_buf = \
                init_enc_buf, init_dec_buf, init_out_buf

        # Generate latent space representation of the input
        x = self.in_conv(x)

        # Generate label embedding
        # l = self.label_embedding(label) # [B, label_len] --> [B, channels]

        # Generate mask corresponding to the label
        m, enc_buf, dec_buf = self.mask_gen(x, enc_buf, dec_buf)

        # Apply mask and decode
        x = x * m
        x = torch.cat((out_buf, x), dim=-1)
        out_buf = x[..., -self.out_buf_len:]
        x = self.out_conv(x)

        # Remove mod padding, if present.
        if mod != 0:
            x = x[:, :, :-mod]


        return x, enc_buf, dec_buf, out_buf

def casual_check(model):
    model.eval()
    torch.manual_seed(1)
    B = 4
    
    n_mics = 7
    chunk_L = 16
    look_ahead_num = 4
    K = 24

    T = (K*chunk_L*2)*10
    input_data = torch.rand(B, n_mics, T)
    y_all= model(input_data)
    enc_buf, dec_buf, out_buf = None, None, None
    for i in range(0, T//chunk_L):
        # if i == 0:
        #     come_data = input_data[:, :, i*chunk_L*K:(i+1)*chunk_L*K + look_ahead_num*chunk_L]
        #     pre_pad = torch.zeros_like( input_data[:, :, i*chunk_L:(i+1)*chunk_L])
        #     come_data = torch.cat((pre_pad, come_data), dim = -1)
        # else:
        #     come_data = input_data[:, :, i*chunk_L*K - chunk_L:(i+1)*chunk_L*K + chunk_L]
        come_data = input_data[:, :, i*chunk_L*K:(i+1)*chunk_L*K + look_ahead_num*chunk_L]
        ref_y = y_all[:, :, i*chunk_L*K:(i+1)*chunk_L*K]

        y, enc_buf, dec_buf, out_buf = model.forward_track(come_data, enc_buf, dec_buf, out_buf)

        check_valid = torch.allclose(y, ref_y, rtol=1e-2)
        print(check_valid)
        if i >= 8:
            raise KeyboardInterrupt





if __name__ == "__main__":
    model_params = {
        "L": 16,
        "n_mics": 6,
        "enc_dim": 512,
        "num_enc_layers": 10,
        "dec_dim": 256,
        "num_dec_layers": 3,
        "dec_buf_len": 24,
        "dec_chunk_size": 24,
        "out_buf_len": 6,
        "use_pos_enc": "true",
        "r": 0.0,
        "fair_compare": True
        }
    model = Network(**model_params)
    '''
    mixed: [B, n_mics, T]
    input audio mixture
    label: [B, num_labels]
    one hot label
    '''
    torch.manual_seed(1)
    casual_check(model)