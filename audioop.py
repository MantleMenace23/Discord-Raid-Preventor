# Fake audioop module stub to bypass ImportError
# Only here because some libraries still try to import audioop.
# This won't actually process audio — just prevents crashes.

def error(*args, **kwargs):
    raise NotImplementedError("audioop is not available in this environment.")

# Common functions that libraries may try to call
add = mul = avg = avgpp = bias = cross = error
findfactor = findfit = lin2lin = max = maxpp = minmax = rms = tostereo = error
tomono = tostereo = ulaw2lin = lin2ulaw = alaw2lin = lin2alaw = error

# Provide version info for compatibility
__version__ = "fake-1.0"
