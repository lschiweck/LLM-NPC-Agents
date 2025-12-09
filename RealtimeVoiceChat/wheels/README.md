This wheel was built on Windows 11 using the `deepspeedpatcher` tool (https://github.com/erew123/deepspeedpatcher) with:

- PyTorch 2.5.1  
- CUDA 12.1  
- Python 3.10.9  
- Enabled options: `CUTLASS_OPS`, `SPARSE_ATTN`, `INFERENCE_CORE_OPS`

To install it, run:

```bash
pip install .\code\deepspeed_wheel\deepspeed-0.16.1+unknown-cp310-cp310-win_amd64.whl
```

I can’t promise it willl work perfectly on every setup.
If you run into issues, I suggest building your own wheel. Just follow the steps in the repo's instructions.