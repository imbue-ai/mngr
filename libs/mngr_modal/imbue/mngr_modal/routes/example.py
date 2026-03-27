import modal

image = modal.Image.debian_slim().uv_pip_install("fastapi[standard]")
vol = modal.Volume.from_name("mngr-8caed3bc40df435fae5817ea0afdbf77-modal-state")
app = modal.App(name="mngr-8caed3bc40df435fae5817ea0afdbf77-modal", image=image)


@app.function(volumes={"/mngr_state": vol})
@modal.fastapi_endpoint(
    # adds interactive documentation in the browser
    docs=True
)
def hello():
    return "Hello world!"
