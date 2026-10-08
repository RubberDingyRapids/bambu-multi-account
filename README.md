# Bambu Multi Account

Use more than one Bambu Lab account in OrcaSlicer at the same time, for
example a work account and a home account. Printers from every account show
up together in the Device tab and the print dialog, and each printer is
driven through the account that owns it. You don't need to log out or
restart OrcaSlicer.

It adds a **Bambu Accounts** tab:

- The account OrcaSlicer is logged in to stays the main one. Presets,
  MakerWorld and the filament library keep using it.
- Add extra accounts with an email code or a password. Accounts that sign
  in with Google or Apple work with the email code.
- Turn each extra account on or off, give it a label (its printers show as
  `[Work] P1S`), or remove it.
- **Make main** swaps an extra account with the main one. This applies
  after a restart.
- Tokens are refreshed automatically before they expire.

Requirements:

- The Open Bambu Networking plugin, logged in to your main Bambu account.
- To run several accounts at once, an Open Bambu Networking library with
  multi-account support (see below). With an older library the tab says so.
  It can still swap the main account, which takes effect after a restart.
- All accounts in the same Bambu region (global or China).

## Install

subscribe to it on the orca cloud plugin hub. keep persano's open bambu
networking plugin installed too, this sits on top of it.

the multi account bit needs a change in the networking library that
persano hasnt merged yet ([PR #2](https://github.com/persano/open-bamboo-networking/pull/2)),
so the hub version of this plugin comes with its own build of his library:
his latest code plus the multi account patch, for windows, linux and mac.
first time you open the tab it says "turn on multi account" with an install
button. that backs up the library you have, puts the multi account one in,
and you restart orca. theres a "put the original back" link if you want to
go back.

it only offers the install if its build is at least as new as your open
bambu networking, so it never rolls back one of his fixes. the builds come
from github actions in [bambu-multi-account](https://github.com/RubberDingyRapids/bambu-multi-account),
which checks his repo every day and makes a new build when he changes
something. if he updates and the build hasnt caught up yet, the tab tells
you and waits.

if you hit install/update in his open bambu tab, his library goes back in
and multi account switches off. just open this tab and install again.

for a local install (`bambu_multi_account_any.py` on its own) theres no
bundled library, so you need a library with multi account in some other
way, like the `obn combined build` folder in this repo.

the first login asks for permission to make HTTP requests to
`api.bambulab.com`.

## How it works

OrcaSlicer only knows one Bambu account. The Open Bambu Networking (OBN)
library keeps that session in `obn.auth.json` in OrcaSlicer's data folder.

This plugin logs the extra accounts in through Bambu's own login API:

- `user/sendemail/code` sends the email code.
- `user/login` exchanges the code or password for tokens.
- `my/profile` fetches the account's details.

It writes the extra accounts to `obn.accounts.json` next to `obn.auth.json`,
in the same layout plus `enabled` and `label`. The plugin is the only
program that writes this file. It also refreshes the tokens through
`user/refreshtoken` when they are within 14 days of expiring.

An OBN library with multi-account support exports
`obn_extra_accounts_api`, `obn_extra_accounts_reload` and
`obn_extra_accounts_status`. The plugin finds the library that OrcaSlicer
already loaded and calls these exports through ctypes. It never loads a
second copy of the library. When the library has these exports:

- It opens one cloud MQTT connection per enabled extra account.
- It appends those accounts' printers to the list it gives OrcaSlicer, and
  remembers which account each printer came from.
- MQTT commands, cloud printing, the camera, renaming and unbinding for a
  printer all use the connection and token of the account that owns it.
- After a reload, it asks OrcaSlicer to fetch the printer list again, using
  the same "printer bound" notice that stock OrcaSlicer reacts to. That
  notice names the printer that is already selected, so the selection does
  not change.

Nothing in OrcaSlicer itself is changed or patched.

If an older OBN library is loaded, **Make main** rewrites `obn.auth.json`
with the chosen account and moves the old main account into the list. OBN
reads `obn.auth.json` once at startup, so the swap applies after a restart.

## Development

```
python -m unittest tests.test_offline -v     # no network, no OrcaSlicer
```

Logs go to OrcaSlicer's stderr with a `[multi_account]` prefix and to
`%APPDATA%\OrcaSlicer\log\python_*.log`. On the library side, look for
`accounts:` and `extra account` lines in `obn.log`.

## Known limits

- The main account can't be changed without a restart. OrcaSlicer itself
  keeps it, and OBN reads it once at startup.
- A printer bound to two of your accounts is shown once, under the first
  account that lists it (the main one first).
- Accounts with two-factor login can't use the password form. Use the
  email code instead.
- Bambu's login API is unofficial and may change.
