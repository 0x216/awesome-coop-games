# Contributing

Corrections are welcome. The list is rebuilt every week by
[`generate.py`](generate.py), so **don't edit README.md**: your change would be
overwritten on the next run. Fixes go into [`overrides.json`](overrides.json),
which the generator applies every time.

## Report a problem

Open an issue if a game has the wrong co-op mode, a misleading description,
or doesn't belong in a section. Include the game's Steam link.

## Send a fix

Edit `overrides.json` and open a pull request. It has three keys:

```json
{
  "exclude": [123456],
  "pin": { "couch": [654321] },
  "note": { "123456": "Your one-line description" }
}
```

- `exclude`: Steam appids to leave out of every section.
- `pin`: section id → appids to put at the top of that section. Pinned games
  count against the section's size, and each game still appears only once.
  Section ids: `online`, `couch`, `couch-4`, `horror`, `cross-platform`,
  `free`. A pinned game must be in imho.run's co-op data (Steam co-op
  category, 500+ reviews).
- `note`: appid → a one-line description that replaces the generated one.
  Write it yourself, in plain words. Don't paste store text, review counts or
  percentages: the check workflow rejects them.

Unknown keys fail validation. You can check your change locally (Python 3.12,
no dependencies):

```sh
python generate.py --check
```

## What we won't merge

- Edits to README.md or `data/list.json` (they are generated).
- Store descriptions, review numbers, images or affiliate links.
- Games without real co-op (competitive multiplayer only).

## Licence

By contributing you agree that your text is published under
[CC BY 4.0](LICENSE-DATA), like the rest of the list, and code changes under
[MIT](LICENSE).
