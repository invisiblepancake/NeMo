# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Callable, Optional, List, Tuple, Literal, Union

import torch
from torch import Tensor
from megatron.core import InferenceParams, parallel_state, tensor_parallel
from megatron.core.fusions.fused_layer_norm import FusedLayerNorm
from megatron.core.models.gpt.gpt_model import GPTModel as MCoreGPTModel
from megatron.core.packed_seq_params import PackedSeqParams
from megatron.core.transformer.attention import Attention
from megatron.core.transformer.custom_layers.transformer_engine import (
    TEColumnParallelLinear,
    TEDotProductAttention,
    TERowParallelLinear,
    TELayerNormColumnParallelLinear,
)
from megatron.core.transformer.enums import AttnMaskType
from megatron.core.transformer.spec_utils import ModuleSpec, build_module
from megatron.core.transformer.transformer_config import TransformerConfig
from megatron.core.utils import (
    make_viewless_tensor,
)
from megatron.core.models.common.embeddings.rotary_pos_embedding import apply_rotary_pos_emb

from torch import nn
from contextlib import nullcontext

from nemo.collections.llm import Llama31Config8B, LlamaConfig
from nemo.collections.llm.gpt.model.base import GPTModel
from nemo.collections.llm.utils import Config
from nemo.lightning import OptimizerModule, io, teardown
from nemo.lightning import get_vocab_size
from nemo.utils import logging

from megatron.core.transformer.transformer_block import TransformerBlock
from megatron.core.transformer.transformer_layer import TransformerLayer, TransformerLayerSubmodules
from megatron.core.transformer.identity_op import IdentityOp
from megatron.core.transformer.mlp import MLP, MLPSubmodules
from megatron.core.transformer.module import MegatronModule
from megatron.core.fusions.fused_bias_dropout import get_bias_dropout_add
from nemo.collections.vlm.llama.model.transformer import _get_full_row_masked_out_mask, get_negative_inf_value

try:
    from megatron.core.transformer.custom_layers.transformer_engine import (
        TEDelayedScaling,
        TENorm,
        get_cpu_offload_context,
        te_checkpoint,
    )

    HAVE_TE = True
    LayerNormImpl = TENorm
except ImportError:
    HAVE_TE = False
    get_cpu_offload_context = None
    try:
        import apex

        LayerNormImpl = FusedLayerNorm
    except ModuleNotFoundError:
        from megatron.core.transformer.torch_layer_norm import WrappedTorchLayerNorm

        LayerNormImpl = WrappedTorchLayerNorm

if TYPE_CHECKING:
    from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec


@dataclass
class LlamaCrossAttentionSubmodules:
    linear_q: Union[ModuleSpec, type] = None
    linear_kv: Union[ModuleSpec, type] = None
    core_attention: Union[ModuleSpec, type] = None
    linear_proj: Union[ModuleSpec, type] = None
    q_layernorm: Union[ModuleSpec, type] = None
    k_layernorm: Union[ModuleSpec, type] = None


class CrossAttentionTextModel(MCoreGPTModel):
    def __init__(
            self,
            config: TransformerConfig,
            transformer_layer_spec: ModuleSpec,
            vocab_size: int,
            max_sequence_length: int,
            pre_process: bool = True,
            post_process: bool = True,
            fp16_lm_cross_entropy: bool = False,
            parallel_output: bool = True,
            share_embeddings_and_output_weights: bool = False,
            position_embedding_type: Literal['learned_absolute', 'rope', 'none'] = 'learned_absolute',
            rotary_percent: float = 1.0,
            rotary_base: int = 10000,
            seq_len_interpolation_factor: Optional[float] = None,
    ):
        super().__init__(config, transformer_layer_spec, vocab_size, max_sequence_length, pre_process, post_process,
                         fp16_lm_cross_entropy, parallel_output,
                         share_embeddings_and_output_weights, position_embedding_type, rotary_percent, rotary_base,
                         seq_len_interpolation_factor)

        # For now we overwrite the self.decoder
        self.decoder = CrossAttentionTransformerBlock(
            config=self.config,
            spec=transformer_layer_spec,
            pre_process=self.pre_process,
            post_process=self.post_process,
        )

        self.learnable_embedding = tensor_parallel.VocabParallelEmbedding(
            num_embeddings=8,
            embedding_dim=self.config.hidden_size,
            init_method=self.config.init_method,
            reduce_scatter_embeddings=False,  # TODO double check this
            config=self.config,
        )

        self.num_frozen_embeddings = self.embedding.word_embeddings.num_embeddings
        self._thresh = self.num_frozen_embeddings - 1

    def _get_xattn_mask(
            self,
            num_tokens,
            text_device,
            text_dtype,
            vision_tokens,
            cross_attention_masks,
    ) -> Tuple[Tensor, Tensor]:
        assert vision_tokens is not None, "Vision tokens must be provided"
        vision_seqlen = vision_tokens.shape[3]
        assert (
                vision_tokens.shape[1] == cross_attention_masks.shape[2]
        ), f"Mismatch in number of images given and number of masks given {vision_tokens.shape} {cross_attention_masks.shape}"
        assert (
                vision_tokens.shape[2] == cross_attention_masks.shape[3]
        ), f"Vision tokens shape {vision_tokens.shape} mismatch with xattn shape {cross_attention_masks.shape}"
        assert (
                num_tokens == cross_attention_masks.shape[1]
        ), f"Mismatch in text sequence length and cross attention mask sequence length {num_tokens} {cross_attention_masks.shape}"
        _, _, _, num_image_tokens, image_token_dim = tuple(vision_tokens.shape)
        bsz, ntext, nimg, nchunks = cross_attention_masks.shape
        cross_attention_masks = (
            cross_attention_masks.repeat_interleave(vision_seqlen, dim=3)
            .view(bsz, ntext, -1)
            .unsqueeze(1)
        )
        full_text_row_masked_out_mask = _get_full_row_masked_out_mask(
            cross_attention_masks,
            get_negative_inf_value(cross_attention_masks.dtype),
        )
        cross_attention_masks *= full_text_row_masked_out_mask

        return (
            cross_attention_masks.to(device=text_device, dtype=text_dtype),
            full_text_row_masked_out_mask.to(device=text_device, dtype=text_dtype),
        )

    def forward(
        self,
        input_ids: Tensor,
        position_ids: Tensor,
        attention_mask: Tensor,
        decoder_input: Tensor = None,
        cross_attention_masks: Tensor = None,
        full_text_row_masked_out_mask: Tensor = None,
        xattn_caches: Tensor = None,
        labels: Tensor = None,
        inference_params: InferenceParams = None,
        packed_seq_params: PackedSeqParams = None,
        extra_block_kwargs: dict = None,
    ) -> Tensor:
        """Forward function of the GPT Model This function passes the input tensors
        through the embedding layer, and then the decoeder and finally into the post
        processing layer (optional).

        It either returns the Loss values if labels are given  or the final hidden units
        """
        # If decoder_input is provided (not None), then input_ids and position_ids are ignored.
        # Otherwise, apply embedding layer on input_ids and position_ids to get decoder_input.

        # Decoder embedding.
        if decoder_input is not None:
            pass
        elif self.pre_process:
            decoder_input = self.embedding(input_ids=input_ids, position_ids=position_ids)
        else:
            # intermediate stage of pipeline
            # decoder will get hidden_states from encoder.input_tensor
            decoder_input = None

        # Rotary positional embeddings (embedding is None for PP intermediate devices)
        rotary_pos_emb = None
        if self.position_embedding_type == 'rope':
            rotary_seq_len = self.rotary_pos_emb.get_rotary_seq_len(
                inference_params, self.decoder, decoder_input, self.config
            )
            rotary_pos_emb = self.rotary_pos_emb(rotary_seq_len)

        # Run decoder.
        hidden_states = self.decoder(
            hidden_states=decoder_input,
            attention_mask=attention_mask,
            inference_params=inference_params,
            rotary_pos_emb=rotary_pos_emb,
            packed_seq_params=packed_seq_params,
            cross_attention_masks=cross_attention_masks,
            full_text_row_masked_out_mask=full_text_row_masked_out_mask,
            xattn_caches=xattn_caches,
            **(extra_block_kwargs or {}),
        )

        if not self.post_process:
            return hidden_states

        # logits and loss
        output_weight = None
        if self.share_embeddings_and_output_weights:
            output_weight = self.shared_embedding_or_output_weight()
        logits, _ = self.output_layer(hidden_states, weight=output_weight)

        if labels is None:
            # [s b h] => [b s h]
            return logits.transpose(0, 1).contiguous()

        loss = self.compute_language_model_loss(labels, logits)

        return loss

    def get_partially_trainable_embedding(self, x, position_ids):
        xz = torch.zeros_like(x, device=x.device)
        oz = torch.ones_like(x, device=x.device)
        x_orig = torch.minimum(x, torch.tensor(self._thresh, device=x.device))
        x_new = (
            torch.maximum(x, torch.tensor(self._thresh + 1, device=x.device))
            - self.num_frozen_embeddings
        )

        mask_orig = torch.where(x >= self.num_frozen_embeddings, xz, oz).unsqueeze(-1).transpose(0, 1)
        mask_new = torch.where(x < self.num_frozen_embeddings, xz, oz).unsqueeze(-1).transpose(0, 1)

        x_orig = self.embedding(x_orig, position_ids)
        x_new = self.learnable_embedding(x_new).type_as(x_orig).transpose(0, 1)
        return x_orig * mask_orig.type_as(x_orig) + x_new * mask_new.type_as(x_new)


class CrossAttentionTransformerBlock(TransformerBlock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # initialize cross attention layers

        self.fusion_schedule = self.config.fusion_schedule
        self.xattn_layers = torch.nn.ModuleList([])  # for state dict to match up with Meta's
        self.xattn_and_dummy_layers = []  # for forward call, not in state dict
        for i in range(self.num_layers_per_pipeline_rank):
            # TODO Handle with PP
            if i in self.fusion_schedule:
                layer_spec = ModuleSpec(
                    module=CrossAttentionTransformerLayer,
                    submodules=TransformerLayerSubmodules(
                        cross_attention=ModuleSpec(
                            module=LlamaCrossAttention,
                            params={"attn_mask_type": AttnMaskType.causal},
                            submodules=LlamaCrossAttentionSubmodules(
                                linear_q=TELayerNormColumnParallelLinear,  # This wraps attention_norm before attention
                                linear_kv=TEColumnParallelLinear,
                                core_attention=TEDotProductAttention,
                                linear_proj=TERowParallelLinear,
                                q_layernorm=TENorm,
                                k_layernorm=TENorm,
                            ),
                        ),
                        cross_attn_bda=get_bias_dropout_add,
                        pre_mlp_layernorm=IdentityOp,
                        mlp=ModuleSpec(
                            module=MLP,
                            submodules=MLPSubmodules(
                                linear_fc1=TELayerNormColumnParallelLinear,  # This wraps ffn_norm before feed_forward
                                linear_fc2=TERowParallelLinear,
                            ),
                        ),
                        mlp_bda=get_bias_dropout_add,
                    ),
                )
                xattn_layer = build_module(layer_spec, config=self.config, layer_number=i + 1)
                self.xattn_layers.append(xattn_layer)
                self.xattn_and_dummy_layers.append(xattn_layer)
            else:
                self.xattn_and_dummy_layers.append(DummyCrossAttentionTransformerLayer(config=self.config))

        assert len(self.xattn_and_dummy_layers) == len(
            self.layers), 'Check PP implementation for cross attention layers!'

    def forward(
            self,
            hidden_states: Tensor,
            attention_mask: Tensor,
            xattn_caches: Tensor = None,
            cross_attention_masks: Tensor = None,
            full_text_row_masked_out_mask: Tensor = None,
            rotary_pos_emb: Tensor = None,
            inference_params: InferenceParams = None,
            packed_seq_params: PackedSeqParams = None,
    ):
        # hidden_states (float): [s, b, h]
        # attention_mask (bool): [1, 1, s, s]

        if not self.pre_process:
            # See set_input_tensor()
            hidden_states = self.input_tensor

        # Viewless tensor.
        # - We only need to create a viewless tensor in the case of micro batch
        #   size (mbs) == 1, since in this case, 'hidden_states.transpose()'
        #   above creates a view tensor, and '.contiguous()' is a pass-through.
        #   For mbs >= 2, '.contiguous()' creates a new tensor, eliminating
        #   the need to make it viewless.
        #
        #   However, we don't explicitly check mbs == 1 here because
        #   make_viewless_tensor() has negligible overhead when its input
        #   is already viewless.
        #
        # - For the 'else' case above, calling make_viewless_tensor() here is
        #   likely redundant, since p2p_communication.py (likely originator)
        #   already creates viewless tensors. That said, make_viewless_tensor()
        #   is called here to be future-proof and corner-case-proof.
        hidden_states = make_viewless_tensor(
            inp=hidden_states,
            requires_grad=True,
            keep_graph=True,
        )

        if self.config.sequence_parallel:
            rng_context = tensor_parallel.get_cuda_rng_tracker().fork()
        else:
            rng_context = nullcontext()

        if self.config.fp8:
            import transformer_engine  # To keep out TE dependency when not training in fp8

            if self.config.fp8 == "e4m3":
                fp8_format = transformer_engine.common.recipe.Format.E4M3
            elif self.config.fp8 == "hybrid":
                fp8_format = transformer_engine.common.recipe.Format.HYBRID
            else:
                raise ValueError("E4M3 and HYBRID are the only supported FP8 formats.")

            fp8_recipe = TEDelayedScaling(
                config=self.config,
                fp8_format=fp8_format,
                override_linear_precision=(False, False, not self.config.fp8_wgrad),
            )
            fp8_group = None
            if parallel_state.model_parallel_is_initialized():
                fp8_group = parallel_state.get_amax_reduction_group(with_context_parallel=True)
            fp8_context = transformer_engine.pytorch.fp8_autocast(
                enabled=True, fp8_recipe=fp8_recipe, fp8_group=fp8_group
            )
        else:
            fp8_context = nullcontext()

        with rng_context and fp8_context:
            # Forward pass.
            if self.config.recompute_granularity == 'full' and self.training:
                raise NotImplementedError
            else:
                for l_no, (layer, xattn_layer) in enumerate(zip(self.layers, self.xattn_and_dummy_layers)):
                    with self.offload_context:
                        if (len(self.cuda_graphs) == 0) or (not self.training):
                            # hidden_states = xattn_layer(
                            #     x=hidden_states,
                            #     xattn_mask=xattn_mask,
                            #     xattn_cache=xattn_caches[l_no//4] # TODO correct mapping 3->0, 7->1, etc, for PP
                            # )
                            hidden_states, context = xattn_layer(
                                hidden_states=hidden_states,
                                cross_attention_masks=cross_attention_masks,
                                xattn_cache=xattn_caches[l_no],
                                full_text_row_masked_out_mask=full_text_row_masked_out_mask,
                                rotary_pos_emb=rotary_pos_emb,
                                inference_params=inference_params,
                                packed_seq_params=packed_seq_params,
                            )
                            hidden_states, context = layer(
                                hidden_states=hidden_states,
                                attention_mask=attention_mask,
                                rotary_pos_emb=rotary_pos_emb,
                                inference_params=inference_params,
                                packed_seq_params=packed_seq_params,
                            )
                            # CUDA graph doesn't output context and is expected to be None
                            assert (
                                    (context is None)
                                    or (not self.config.enable_cuda_graph)
                                    or (not self.training)
                            )
                        else:
                            # CUDA graph replay for layer `l_no` and microbatch `self.current_microbatch`
                            # CUDA graph requires positional arguments with the exception of is_first_microbatch.
                            # Also CUDA graph accepts only Tensor inputs and outputs. Hence, the arg list and
                            # returned list is limited to `hidden_states`.
                            assert (len(self.cuda_graphs) > l_no) and (
                                    self.current_microbatch < len(self.cuda_graphs[l_no])
                            )
                            hidden_states = self.cuda_graphs[l_no][self.current_microbatch](
                                hidden_states, is_first_microbatch=(self.current_microbatch == 0)
                            )

                    if (
                            torch.is_grad_enabled()
                            and self.config.cpu_offloading
                            and self.group_prefetch_offload_commit_async is not None
                    ):
                        hidden_states = self.group_prefetch_offload_commit_async(hidden_states)

        # Final layer norm.
        if self.final_layernorm is not None:
            hidden_states = self.final_layernorm(hidden_states)
            # TENorm produces a "viewed" tensor. This will result in schedule.py's
            # deallocate_output_tensor() throwing an error, so a viewless tensor is
            # created to prevent this.
            hidden_states = make_viewless_tensor(
                inp=hidden_states, requires_grad=True, keep_graph=True
            )

        return hidden_states


class CrossAttentionTransformerLayer(TransformerLayer):
    def __init__(
            self,
            config: TransformerConfig,
            submodules: TransformerLayerSubmodules,
            layer_number: int = 1,
            hidden_dropout: float = None,
    ):
        super().__init__(
            config=config,
            submodules=submodules,
            layer_number=layer_number,
            hidden_dropout=hidden_dropout,
        )

        self.gate_attn = nn.Parameter(torch.zeros(1))
        self.gate_ffn = nn.Parameter(torch.zeros(1))

    def compute_xattn_kv_cache(self, xattn_tokens: Tensor) -> Tensor:
        return self.cross_attention._compute_xattn_kv_cache(xattn_tokens)

    def forward(
            self,
            hidden_states,
            cross_attention_masks,
            xattn_cache=None,
            full_text_row_masked_out_mask=None,
            rotary_pos_emb=None,
            inference_params=None,
            packed_seq_params=None,
    ):
        # hidden_states: [s, b, h]

        # Residual connection.
        residual = hidden_states

        # Optional Layer norm after self-attention
        pre_cross_attn_layernorm_output = self.pre_cross_attn_layernorm(hidden_states)

        # Cross attention.
        attention_output_with_bias = self.cross_attention(
            pre_cross_attn_layernorm_output,
            cross_attention_masks=cross_attention_masks,
            xattn_cache=xattn_cache,
            full_text_row_masked_out_mask=full_text_row_masked_out_mask,
            rotary_pos_emb=rotary_pos_emb,
            inference_params=inference_params,
        )

        _gate_attn = self.gate_attn.tanh()
        assert isinstance(attention_output_with_bias,
                          tuple), "`attention_output_with_bias` needs to be tuple for gating."
        attention_output_with_bias = tuple(
            _gate_attn * output if output is not None else None
            for output in attention_output_with_bias
        )

        # TODO: could we move `bias_dropout_add_exec_handler` itself
        # inside the module provided in the `bias_dropout_add_spec` module?
        with self.bias_dropout_add_exec_handler():
            hidden_states = self.cross_attn_bda(self.training, self.config.bias_dropout_fusion)(
                attention_output_with_bias, residual, self.hidden_dropout
            )

        # Residual connection.
        residual = hidden_states

        # Optional Layer norm post the cross-attention.
        pre_mlp_layernorm_output = self.pre_mlp_layernorm(hidden_states)

        # MLP.
        mlp_output_with_bias = self.mlp(pre_mlp_layernorm_output)

        _gate_ffn = self.gate_ffn.tanh()
        assert isinstance(mlp_output_with_bias,
                          tuple), "`mlp_output_with_bias` needs to be tuple for gating."
        mlp_output_with_bias = tuple(
            _gate_attn * output if output is not None else None
            for output in mlp_output_with_bias
        )

        # TODO: could we move `bias_dropout_add_exec_handler` itself
        # inside the module provided in the `bias_dropout_add_spec` module?
        with self.bias_dropout_add_exec_handler():
            hidden_states = self.mlp_bda(self.training, self.config.bias_dropout_fusion)(
                mlp_output_with_bias, residual, self.hidden_dropout
            )

        # Jit compiled function creates 'view' tensor. This tensor
        # potentially gets saved in the MPU checkpoint function context,
        # which rejects view tensors. While making a viewless tensor here
        # won't result in memory savings (like the data loader, or
        # p2p_communication), it serves to document the origin of this
        # 'view' tensor.
        output = make_viewless_tensor(
            inp=hidden_states, requires_grad=hidden_states.requires_grad, keep_graph=True
        )

        return output, None  # context


class DummyCrossAttentionTransformerLayer(MegatronModule):
    """Dummy cross-attention transformer block with tanh-gated attention and feedforward."""

    def __call__(
            self,
            hidden_states: Tensor,
            *args,
            **kwargs,
    ) -> Tensor:
        return hidden_states


class LlamaCrossAttention(Attention):
    """Cross-attention layer class for Llama VLM support

    Cross-attention layer takes input with size [s, b, h] and context with size
    [s, b, h] and returns output of the same size.
    """

    def __init__(
            self,
            config: TransformerConfig,
            submodules: LlamaCrossAttentionSubmodules,
            layer_number: int,
            attn_mask_type=AttnMaskType.padding,
    ):
        super().__init__(
            config=config,
            submodules=submodules,
            layer_number=layer_number,
            attn_mask_type=attn_mask_type,
            attention_type="cross",
        )

        # TODO might need special care when TP>8
        assert self.query_projection_size % self.kv_projection_size == 0

        self.n_rep = self.query_projection_size // self.kv_projection_size
        self.linear_q = build_module(
            submodules.linear_q,
            self.config.hidden_size,
            self.query_projection_size,
            config=self.config,
            init_method=self.config.init_method,
            gather_output=False,
            bias=self.config.add_bias_linear,
            skip_bias_add=False,
            is_expert=False,
        )

        self.linear_kv = build_module(
            submodules.linear_kv,
            self.config.hidden_size,
            2 * self.kv_projection_size,
            config=self.config,
            init_method=self.config.init_method,
            gather_output=False,
            bias=self.config.add_bias_linear,
            skip_bias_add=False,
            is_expert=False,
        )

        self.q_layernorm = build_module(
            submodules.q_layernorm,
            hidden_size=self.hidden_size_per_attention_head,
            config=self.config,
            eps=self.config.layernorm_epsilon,
        )

        self.k_layernorm = build_module(
            submodules.k_layernorm,
            hidden_size=self.hidden_size_per_attention_head,
            config=self.config,
            eps=self.config.layernorm_epsilon,
        )

    def get_key_value_tensors(self, key_value_states):
        # Attention heads [sk, b, h] --> [sk, b, (np * 2 * hn)]
        mixed_kv, _ = self.linear_kv(key_value_states)

        # [sk, b, (np * 2 * hn)] --> [sk, b, np, 2 * hn]
        new_tensor_shape = mixed_kv.size()[:-1] + (
            self.num_query_groups_per_partition,  # TODO(yuya): check TP
            2 * self.hidden_size_per_attention_head,
        )
        mixed_kv = mixed_kv.view(*new_tensor_shape)

        # [sk, b, np, 2 * hn] --> 2 [sk, b, np, hn]
        (key, value) = tensor_parallel.split_tensor_along_last_dim(mixed_kv, 2)

        # repeat k/v heads if n_kv_heads < n_heads
        # key = key.repeat_interleave(self.n_rep, dim=2)
        # value = value.repeat_interleave(self.n_rep, dim=2)

        # Apply LayerNorm
        key = self.k_layernorm(key)

        return key, value

    def get_query_tensor(self, hidden_states):

        # Attention head [sq, b, h] --> [sq, b, hp]
        query, _ = self.linear_q(hidden_states)

        # [sq, b, hp] --> [sq, b, np, hn]
        new_tensor_shape = query.size()[:-1] + (
            self.num_attention_heads_per_partition,
            self.hidden_size_per_attention_head,
        )
        query = query.view(*new_tensor_shape)

        # Apply LayerNorm
        query = self.q_layernorm(query)

        return query

    def get_query_key_value_tensors(self, hidden_states, key_value_states):
        query = self.get_query_tensor(hidden_states)
        key, value = self.get_key_value_tensors(key_value_states)
        return query, key, value

    def forward(
            self,
            hidden_states,
            cross_attention_masks,
            xattn_cache=None,
            full_text_row_masked_out_mask=None,
            inference_params=None,
            rotary_pos_emb=None,
            packed_seq_params=None,
    ):
        # hidden_states: [sq, b, h]

        # For self attention we just duplicate the rotary_pos_emb if it isn't already
        if rotary_pos_emb is not None and not isinstance(rotary_pos_emb, tuple):
            rotary_pos_emb = (rotary_pos_emb,) * 2

        # =====================
        # Query, Key, and Value
        # =====================
        # Get the query, key and value tensors based on the type of attention -
        # self or cross attn.
        query = self.get_query_tensor(hidden_states)
        key, value = xattn_cache

        # ===================================================
        # Adjust key, value, and rotary_pos_emb for inference
        # ===================================================
        key, value, rotary_pos_emb, attn_mask_type = self._adjust_key_value_for_inference(
            inference_params, key, value, rotary_pos_emb
        )

        if packed_seq_params is not None:
            query = query.squeeze(1)
            key = key.squeeze(1)
            value = value.squeeze(1)

        # ================================================
        # relative positional embedding (rotary embedding)
        # ================================================
        if rotary_pos_emb is not None:
            q_pos_emb, k_pos_emb = rotary_pos_emb

            if packed_seq_params is not None:
                cu_seqlens_q = packed_seq_params.cu_seqlens_q
                cu_seqlens_kv = packed_seq_params.cu_seqlens_kv
            else:
                cu_seqlens_q = cu_seqlens_kv = None

            # query = apply_rotary_pos_emb(
            #     query,
            #     q_pos_emb,
            #     config=self.config,
            #     cu_seqlens=cu_seqlens_q,
            # )
            #
            #
            # key = apply_rotary_pos_emb(
            #     key,
            #     k_pos_emb,
            #     config=self.config,
            #     cu_seqlens=cu_seqlens_kv,
            # )

            # TODO, can apply positional embedding to value_layer so it has
            # absolute positional embedding.
            # otherwise, only relative positional embedding takes effect
            # value_layer = apply_rotary_pos_emb(value_layer, k_pos_emb)

        # ==================================
        # core attention computation
        # ==================================

        if self.checkpoint_core_attention and self.training:
            core_attn_out = self._checkpointed_attention_forward(
                query,
                key,
                value,
                cross_attention_masks,
                attn_mask_type=attn_mask_type,
                packed_seq_params=packed_seq_params,
            )
        else:
            core_attn_out = self.core_attention(
                query,
                key,
                value,
                cross_attention_masks,
                attn_mask_type=attn_mask_type,
                packed_seq_params=packed_seq_params,
            )

        if packed_seq_params is not None:
            # reshape to same output shape as unpacked case
            # (t, np, hn) -> (t, b=1, h=np*hn)
            # t is the pack size = sum (sq_i)
            # note that batch is a dummy dimension in the packed case
            core_attn_out = core_attn_out.reshape(core_attn_out.size(0), 1, -1)

        # TODO(yuya): find a better place for transpose
        # [b, head, s, dim]
        full_text_row_masked_out_mask = full_text_row_masked_out_mask.permute(2, 0, 1, 3).squeeze(2)
        core_attn_out = core_attn_out * full_text_row_masked_out_mask

        # =================
        # Output. [sq, b, h]
        # =================

        output, bias = self.linear_proj(core_attn_out)

        return output, bias

    def _compute_xattn_kv_cache(self, xattn_tokens: Tensor) -> Tensor:
        key, value = self.get_key_value_tensors(xattn_tokens)
        return torch.stack([key, value])


class LlamaModel(GPTModel):
    def __init__(
            self,
            config: Annotated[Optional[LlamaConfig], Config[LlamaConfig]] = None,
            optim: Optional[OptimizerModule] = None,
            tokenizer: Optional["TokenizerSpec"] = None,
            model_transform: Optional[Callable[[nn.Module], nn.Module]] = None,
    ):
        super().__init__(config or LlamaConfig(), optim=optim, tokenizer=tokenizer, model_transform=model_transform)


# @io.model_exporter(LlamaModel, "hf")
# class HFLlamaExporter(io.ModelConnector[LlamaModel, "LlamaForCausalLM"]):
#     def init(self) -> "LlamaForCausalLM":
#         from transformers import AutoModelForCausalLM
#
#         return AutoModelForCausalLM.from_config(self.config)
#
#     def apply(self, output_path: Path) -> Path:
#         target = self.init()
#         source, _ = self.nemo_load(str(self))
#         target = self.convert_state(source, target)
#
#         target = target.cpu()
#         target.save_pretrained(output_path)
#         self.tokenizer.save_pretrained(output_path)
#
#         return output_path
#
#     def convert_state(self, source, target):
#         mapping = {
#             "embedding.word_embeddings.weight": "model.embed_tokens.weight",
#             "decoder.layers.*.self_attention.linear_proj.weight": "model.layers.*.self_attn.o_proj.weight",
#             "decoder.layers.*.mlp.linear_fc2.weight": "model.layers.*.mlp.down_proj.weight",
#             "decoder.layers.*.self_attention.linear_qkv.layer_norm_weight": "model.layers.*.input_layernorm.weight",
#             "decoder.layers.*.mlp.linear_fc1.layer_norm_weight": "model.layers.*.post_attention_layernorm.weight",
#             "decoder.final_layernorm.weight": "model.norm.weight",
#             "output_layer.weight": "lm_head.weight",
#         }
#
#         return io.apply_transforms(source, target, mapping=mapping, transforms=[_export_qkv, _export_linear_fc1])
#
#     @property
#     def tokenizer(self):
#         return io.load_context(str(self)).model.tokenizer.tokenizer
#
#     @property
#     def config(self) -> "HFLlamaConfig":
#         source: LlamaConfig = io.load_context(str(self)).model.config
#
#         from transformers import LlamaConfig as HFLlamaConfig
#
#         return HFLlamaConfig(
#             num_hidden_layers=source.num_layers,
#             hidden_size=source.hidden_size,
#             intermediate_size=source.ffn_hidden_size,
#             num_attention_heads=source.num_attention_heads,
#             max_position_embeddings=source.seq_length,
#             initializer_range=source.init_method_std,
#             rms_norm_eps=source.layernorm_epsilon,
#             num_key_value_heads=source.num_query_groups,
#             rope_theta=source.rotary_base,
#             vocab_size=self.tokenizer.vocab_size,
#         )
#
#
# @io.state_transform(
#     source_key=(
#             "model.layers.*.self_attn.q_proj.weight",
#             "model.layers.*.self_attn.k_proj.weight",
#             "model.layers.*.self_attn.v_proj.weight",
#     ),
#     target_key="decoder.layers.*.self_attention.linear_qkv.weight",
# )
# def _import_qkv(ctx: io.TransformCTX, q, k, v):
#     megatron_config = ctx.target.config
#
#     head_num = megatron_config.num_attention_heads
#     num_query_groups = megatron_config.num_query_groups
#     heads_per_group = head_num // num_query_groups
#     hidden_size = megatron_config.hidden_size
#     head_num = megatron_config.num_attention_heads
#     head_size = hidden_size // head_num
#
#     old_tensor_shape = q.size()
#     new_q_tensor_shape = (head_num, head_size) + old_tensor_shape[1:]
#     new_kv_tensor_shape = (num_query_groups, head_size) + old_tensor_shape[1:]
#
#     q = q.view(*new_q_tensor_shape)
#     k = k.view(*new_kv_tensor_shape)
#     v = v.view(*new_kv_tensor_shape)
#
#     qkv_weights_l = []
#     for i in range(num_query_groups):
#         qkv_weights_l.append(q[i * heads_per_group: (i + 1) * heads_per_group, :, :])
#         qkv_weights_l.append(k[i: i + 1, :, :])
#         qkv_weights_l.append(v[i: i + 1, :, :])
#     qkv_weights = torch.cat(qkv_weights_l)
#     assert qkv_weights.ndim == 3, qkv_weights.shape
#     assert qkv_weights.shape[0] == (heads_per_group + 2) * num_query_groups, qkv_weights.shape
#     assert qkv_weights.shape[1] == head_size, qkv_weights.shape
#     assert qkv_weights.shape[2] == old_tensor_shape[1], qkv_weights.shape
#
#     qkv_weights = qkv_weights.reshape([head_size * (head_num + 2 * num_query_groups), hidden_size])
#
#     return qkv_weights
#
#
# @io.state_transform(
#     source_key="decoder.layers.*.self_attention.linear_qkv.weight",
#     target_key=(
#             "model.layers.*.self_attn.q_proj.weight",
#             "model.layers.*.self_attn.k_proj.weight",
#             "model.layers.*.self_attn.v_proj.weight",
#     ),
# )
# def _export_qkv(ctx: io.TransformCTX, linear_qkv):
#     megatron_config = ctx.source.config
#
#     head_num = megatron_config.num_attention_heads
#     num_query_groups = megatron_config.num_query_groups
#     heads_per_group = head_num // num_query_groups
#     hidden_size = megatron_config.hidden_size
#     head_num = megatron_config.num_attention_heads
#     head_size = hidden_size // head_num
#     qkv_total_dim = head_num + 2 * num_query_groups
#
#     linear_qkv = linear_qkv.reshape([qkv_total_dim, head_size, hidden_size])
#     q_slice = torch.cat(
#         [
#             torch.arange((heads_per_group + 2) * i, (heads_per_group + 2) * i + heads_per_group)
#             for i in range(num_query_groups)
#         ]
#     )
#     k_slice = torch.arange(heads_per_group, qkv_total_dim, (heads_per_group + 2))
#     v_slice = torch.arange(heads_per_group + 1, qkv_total_dim, (heads_per_group + 2))
#
#     q_proj = linear_qkv[q_slice].reshape(-1, hidden_size).cpu()
#     k_proj = linear_qkv[k_slice].reshape(-1, hidden_size).cpu()
#     v_proj = linear_qkv[v_slice].reshape(-1, hidden_size).cpu()
#
#     return q_proj, k_proj, v_proj
#
#
# @io.state_transform(
#     source_key=("model.layers.*.mlp.gate_proj.weight", "model.layers.*.mlp.up_proj.weight"),
#     target_key="decoder.layers.*.mlp.linear_fc1.weight",
# )
# def _import_linear_fc1(down, gate):
#     return torch.cat((down, gate), axis=0).float()
#
#
# @io.state_transform(
#     source_key="decoder.layers.*.mlp.linear_fc1.weight",
#     target_key=("model.layers.*.mlp.gate_proj.weight", "model.layers.*.mlp.up_proj.weight"),
# )
# def _export_linear_fc1(linear_fc1):
#     gate_proj, up_proj = torch.chunk(linear_fc1, 2, dim=0)
#
#     return gate_proj, up_proj


def apply_rope_scaling(
        inv_freq,
        factor: int = 8,
        low_freq_factor: int = 1,
        high_freq_factor: int = 4,
        old_context_len: int = 8192,
):
    logging.info(
        f"Apply rope scaling with factor={factor}, low_freq_factor={low_freq_factor}, high_freq_factor={high_freq_factor}, old_context_len={old_context_len}."
    )

    low_freq_wavelen = old_context_len / low_freq_factor
    high_freq_wavelen = old_context_len / high_freq_factor

    wavelen = 2 * math.pi / inv_freq
    # wavelen < high_freq_wavelen: do nothing
    # wavelen > low_freq_wavelen: divide by factor
    inv_freq_llama = torch.where(wavelen > low_freq_wavelen, inv_freq / factor, inv_freq)
    # otherwise: interpolate between the two, using a smooth factor
    smooth_factor = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
    smoothed_inv_freq = (1 - smooth_factor) * inv_freq_llama / factor + smooth_factor * inv_freq_llama
    is_medium_freq = ~(wavelen < high_freq_wavelen) * ~(wavelen > low_freq_wavelen)
    inv_freq_llama = torch.where(is_medium_freq, smoothed_inv_freq, inv_freq_llama)

    return inv_freq_llama
