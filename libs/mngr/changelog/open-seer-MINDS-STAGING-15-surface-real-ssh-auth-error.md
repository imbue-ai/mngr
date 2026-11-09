Added `ssh_utils.load_ssh_private_key_file`, which loads an SSH private key of any type paramiko supports, detecting the type from the file contents (`paramiko.PKey.from_path`). Handing paramiko a key path (`SSHClient.connect(key_filename=...)`) or trying key classes one at a time makes paramiko report only the last key class's failure, which is how a rejected RSA key came to be reported as `encountered RSA key, expected OPENSSH key`. Loading the key once means any later failure is reported as itself.

A key that cannot be loaded raises one of two errors, both subclasses of the new `SSHPrivateKeyError` (a `MngrError`):

- `SSHPrivateKeyEncryptedError` when the key is passphrase-protected, which mngr has no passphrase for. This is read from the file's own encryption marker (the PKCS#8 PEM label, the classic-PEM `Proc-Type` header, or the cipher named in the OpenSSH container), not from the `TypeError` `cryptography` raises for it, because `cryptography` raises the same `TypeError` for a bad argument, which is a bug and must not be reported as a bad key file.

- `SSHPrivateKeyUnparseableError` when the file cannot be read, is malformed, or holds a key type mngr cannot use.
