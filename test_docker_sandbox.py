"""Minimal repro: Modal's enable_docker=True does not provision a Docker daemon."""
import modal

modal.enable_output()

app = modal.App.lookup("test-docker", create_if_missing=True)
sb = modal.Sandbox.create(
    app=app,
    image=modal.Image.debian_slim(),
    timeout=60,
    experimental_options={"enable_docker": True},
)

p = sb.exec("sh", "-c", "ls /var/run/docker.sock 2>&1; which docker 2>&1")
p.wait()
print(p.stdout.read())

sb.terminate()
