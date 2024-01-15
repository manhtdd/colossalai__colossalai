import pytest
import torch
from packaging import version

from colossalai.kernel.triton import context_attention_unpadded
from colossalai.utils import get_current_device
from tests.test_infer_ops.triton.kernel_utils import mock_alloc_block_table_and_kvcache, torch_attn_ref

try:
    import triton  # noqa

    HAS_TRITON = True
except ImportError:
    HAS_TRITON = False
    print("please install triton from https://github.com/openai/triton")

TRITON_CUDA_SUPPORT = version.parse(torch.version.cuda) > version.parse("11.4")


def torch_attn_unpad(
    q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, context_lengths: torch.Tensor, num_heads: int, num_kv_heads: int
):
    # Process sequence one by one and concatenate them together.
    # q,k,v [num_tokens(sum(context_lengths)), num_heads, head_dim]
    assert context_lengths.dim() == 1, "context_lengths should be a 1D tensor"

    _, num_heads, head_dim = q.shape
    out_torch = []
    start_idx = 0
    for seq_i in range(len(context_lengths)):
        end_idx = start_idx + context_lengths[seq_i].item()
        seq_len = end_idx - start_idx
        mask = torch.tril(torch.ones(1, 1, seq_len, seq_len), diagonal=0).to(device=q.device)
        mask[mask == 0.0] = float("-inf")

        torch_attn_ref_out = torch_attn_ref(
            q[start_idx:end_idx].unsqueeze(0),
            k[start_idx:end_idx].unsqueeze(0),
            v[start_idx:end_idx].unsqueeze(0),
            mask,
            1,  # set bsz as 1 as we're processing sequence one by one
            seq_len,
            seq_len,
            num_heads,
            num_kv_heads,
            head_dim,
        )
        out_torch.append(torch_attn_ref_out.squeeze(0))
        start_idx = end_idx

    return torch.cat(out_torch, dim=0)


@pytest.mark.skipif(not (HAS_TRITON and TRITON_CUDA_SUPPORT), reason="requires triton")
@pytest.mark.parametrize("bsz", [4, 7, 32])
@pytest.mark.parametrize("block_size", [16, 32, 64])
@pytest.mark.parametrize("max_num_blocks_per_seq", [8, 32])
@pytest.mark.parametrize("num_attn_heads", [16])
@pytest.mark.parametrize("kv_group_num", [1, 2, 16])
@pytest.mark.parametrize("same_context_len", [True, False])
def test_context_attention(
    bsz: int,
    block_size: int,
    max_num_blocks_per_seq: int,
    num_attn_heads: int,
    kv_group_num: int,
    same_context_len: bool,
):
    torch.manual_seed(123)
    # It's necessary to clear cache here.
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()

    num_kv_heads = num_attn_heads // kv_group_num
    assert isinstance(num_kv_heads, int) and num_kv_heads > 0, "Invalid number of kv heads."
    head_dim = 32
    max_seq_len = max_num_blocks_per_seq * block_size
    dtype = torch.float16
    device = get_current_device()

    if same_context_len:
        context_lengths = torch.tensor([max_seq_len for _ in range(bsz)], dtype=torch.int32, device=device)
    else:
        context_lengths = torch.randint(low=1, high=max_seq_len, size=(bsz,), dtype=torch.int32, device=device)
    num_tokens = torch.sum(context_lengths).item()

    qkv_size = (num_tokens, num_attn_heads + 2 * num_kv_heads, head_dim)
    qkv = torch.empty(size=qkv_size, dtype=dtype, device=device).normal_(mean=0.0, std=0.5)
    q, k, v = torch.split(qkv, [num_attn_heads, num_kv_heads, num_kv_heads], dim=-2)

    cache_shape = (bsz * max_num_blocks_per_seq, num_kv_heads, head_dim, block_size)
    k_cache_torch = torch.zeros(size=cache_shape, dtype=dtype, device=device)
    k_cache_triton = torch.zeros_like(k_cache_torch)
    v_cache_torch = torch.zeros(size=cache_shape, dtype=dtype, device=device)
    v_cache_triton = torch.zeros_like(v_cache_torch)

    # Mock allocation on block tables
    block_tables = mock_alloc_block_table_and_kvcache(
        k, v, k_cache_torch, v_cache_torch, context_lengths, bsz, max_num_blocks_per_seq, block_size
    )
    block_tables = block_tables.to(device=device)
    out_triton = context_attention_unpadded(
        q, k, v, k_cache_triton, v_cache_triton, context_lengths, block_tables, block_size
    )

    out_torch = torch_attn_unpad(q, k, v, context_lengths, num_attn_heads, num_kv_heads)

    assert out_torch.shape == out_triton.shape
    assert torch.allclose(out_torch, out_triton, atol=1e-3, rtol=1e-4)
    assert torch.allclose(k_cache_torch, k_cache_triton)
    assert torch.allclose(v_cache_torch, v_cache_triton)