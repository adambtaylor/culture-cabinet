# API key box MVP

One file contains the encrypted OpenAI API key. Culture Cabinet's unlock key is
stored in the password-protected GNOME Login keyring through standard libsecret.
The application retrieves that unlock key to decrypt the file using the standard
`cryptography.fernet.Fernet` implementation. There is no database, inode binding,
recovery journal, second password protocol, provider request or custom cipher.

This is a separate MVP entry point and application adapter. The existing Cabinet
database and older keyring collection are untouched. The legacy web application's
startup and inference path are not yet switched to this adapter.

## Private entry and restart check

With a password-protected GNOME **Login** keyring already configured, run in your
own terminal from this checkout:

```sh
/usr/bin/python3 -I -B tools/key_box.py save
```

Paste the API key into the hidden prompt. GNOME may ask you to unlock its keyring.
The file is `~/.local/share/culture-cabinet-key-box/openai.fernet` (0600 in a 0700
folder). The command exits when saving finishes. Close the terminal, open a new
one in the checkout, and run:

```sh
/usr/bin/python3 -I -B tools/key_box.py check
```

This decrypts the file and reports success without printing the key or contacting
OpenAI. Do not pass the key as a command argument or environment variable. The
internal `_save` / `_load` commands are private pipe transports for the adapter.

## Application use

```python
from shelf.key_box import save_openai_key, load_openai_key

save_openai_key(key_from_private_input)
api_key = load_openai_key()  # Pass directly to the authorized backend consumer.
```

The adapter works in the existing application environment by using the installed
system Python's GI/libsecret and cryptography packages through captured pipes.
Plaintext is briefly present in application memory; core dumps are disabled in
the receiving process and helper. Python does not guarantee erasure of strings.
The OS user account and unlocked desktop session remain the custody boundary.

The Login keyring's encrypted format header is required before accessing its
unlock key. Missing/locked/unavailable OS storage, invalid ciphertext or missing
unlock keys refuse; an existing box is never reset by generating a new unlock
key. Save replaces a valid existing API key atomically. Losing the OS unlock key
means losing access to the box: there is no recovery bypass.

## Evidence

`/usr/bin/python3 -B -m unittest discover -s tools -p test_key_box.py -v`
uses disposable files, real Fernet encryption and a test keyring. The fresh-process
test demonstrates encrypted-file retrieval in another process, not actual GNOME
custody. Real OS save/retrieve and owner key entry must be reported separately.
