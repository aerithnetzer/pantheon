# pantheon

To build this project, run

```bash
pyinstaller --windowed --name "Pantheon" main.py
```

Then, to target macOS (dmg), run:

```bash
create-dmg Pantheon.dmg dist/Pantheon/Pantheon
```

Dependencies:

- `create-dmg`: `brew install create-dmg`
