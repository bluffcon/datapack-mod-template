# datapack mod template

for internal use. makes fabric and neoforge jars out of datapacks and uploads them to modrinth

needs:

1. `mod.json` (with that exact name)
2. `.github/workflows/release.yml` from this repo's root (not this repos workflows)
3. repo secret `MODRINTH_TOKEN`

controls: commit with a message starting with `!` and followed with a coax versioning number (`!r7 Commit message`)

mainly made for datapack families like arbiterlib. not a public repo but 