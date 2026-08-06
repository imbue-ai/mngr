`test_prevent_getattr` in `utils/test_ratchets.py` is marked
`@pytest.mark.flaky`: the tree-wide regex scan occasionally blows the 10s
pytest-timeout on a cold-cache offload run (sandbox I/O saturated by the base
image build) and passes on retry.
