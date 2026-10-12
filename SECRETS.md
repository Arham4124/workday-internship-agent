# Secrets: Gmail app password, Workday password, and keeping them away from Claude

The agent needs two secrets:

| Secret | Why | Required? |
|---|---|---|
| **Gmail app password** | read Workday verification / password-reset e-mails (IMAP) and send you the daily summary (SMTP) | yes |
| **Your usual Workday password** | sign in where you already have Workday accounts | only if you already have accounts |

It does **not** need a Claude API key or token: it runs the `claude` command, which uses your existing Claude Code
login (`claude auth status` should say `"loggedIn": true`; log in with `claude` → `/login` if not).

**Golden rule: never type a password into a chat with Claude, into a config file, or into a GitHub issue.**
Chat text is sent to Claude and may be stored under your account's data settings; config files can end up in git.

---

## 1. Create the Gmail app password

An app password is a 16-letter password that lets one program use your Gmail without your real password. It can
be revoked at any time without changing your Google password.

1. Use a **personal Gmail account** (school and work accounts often block app passwords).
2. Turn on **2-Step Verification**: go to <https://myaccount.google.com/security> → *How you sign in to Google* →
   *2-Step Verification* → follow the steps.
3. Go to <https://myaccount.google.com/apppasswords> (sign in again if asked).
4. Type a name such as `JobAgent` and click **Create**.
5. Google shows a 16-letter password **once** (e.g. `abcd efgh ijkl mnop`). Keep the window open for step 3 below.
   Spaces don't matter.

If Gmail ever reports that IMAP is disabled: Gmail → ⚙ → *See all settings* → *Forwarding and POP/IMAP* →
*Enable IMAP*.

## 2. (Optional) Your usual Workday password

If you already applied at some companies through Workday with this e-mail and mostly used one password, the agent
tries it first at each company. If it doesn't work, the agent creates a new account (unique random password) or,
when an account already exists, uses **Forgot password** through your Gmail and saves the new password.
`./agent.sh login <company>` shows the login it uses (it refuses unless you type it yourself in Terminal).

## 3. Enter the secrets privately

Do this in the **Terminal app**, not in a chat with Claude.

**Option A — private file (lets you paste and review)**

```bash
cd workday-internship-agent
./agent.sh secrets-template        # creates ~/.jobagent_secrets with permissions 600 (only you can read it)
nano ~/.jobagent_secrets           # type after the = signs; save with Control+O, Return, Control+X
./agent.sh import-secrets          # -> Keychain, tests the Gmail login, then overwrites and deletes the file
```

The file looks like this while you edit it:

```
GMAIL_APP_PASSWORD=abcdefghijklmnop
WORKDAY_PASSWORD=your-usual-workday-password
```

`import-secrets` prints only the lengths of what it saved (e.g. "16 characters") and "Gmail login test: ok".

Use **nano**, not TextEdit or another editor with autosave/versions — those can keep hidden copies of the file.
The file lives in your home folder, not in the project, so it can never be committed.

**Option B — hidden prompts (nothing written to disk)**

```bash
./agent.sh setup    # asks for each password with hidden typing and stores it in the Keychain
```

## 4. Where they live and who can see them

- Stored in the **macOS Keychain** under the service name **`InternshipAgent`** (items `gmail-app-password`,
  `workday-common`, and `workday:<company>` for passwords the agent created or reset).
  View them in the Keychain Access app (it asks for your Mac password).
- The agent reads them only to type a password into a Workday login box, or to log in to Gmail over IMAP/SMTP.
- They are **never** put into a Claude prompt: page snapshots sent to Claude skip password fields, and sign-in is
  done by plain code, not by Claude.
- They are never written to logs, the database, or `data/runs/`.

**Lock them down further (optional):** in Keychain Access, open each `InternshipAgent` item → *Access Control* →
*Confirm before allowing access*. macOS will then ask you before any program reads them — including the agent,
so unattended daily runs will wait for your click.

## 5. Working with Claude Code on this repository

- Claude Code asks before running commands (unless you turned that off). Deny anything that reads
  `~/.jobagent_secrets`, runs `security find-generic-password`, or calls `./agent.sh login`.
- Don't paste `./agent.sh login` output into a chat.
- Your *profile* (name, address, education, self-ID answers) **is** sent to Claude when filling forms — that is
  how the forms get filled. Review your Claude account's privacy settings if that matters to you.

## 6. Revoke or replace

- **Gmail:** delete the `JobAgent` app password at <https://myaccount.google.com/apppasswords>. Create a new one
  and run `./agent.sh secrets-template` + `import-secrets` again to replace it.
- **Workday:** change the password on the company's site, then run `import-secrets` again (or let the agent reset it).
- **Remove everything from the Keychain:** Keychain Access → search `InternshipAgent` → delete the items.

## 7. Before you push to GitHub

`.gitignore` already excludes `config/profile.yaml`, `config/answers.yaml`, `config/settings.yaml`,
`documents/`, `data/` and `Dashboard.html`. Still run `git status` before every commit and make sure none of your
personal files are listed.
