# Running the desk with zero Claude dependency

Everything the bot does to trade -- scan the market, size a trade, manage
stops, retrain itself nightly -- is plain Python. Claude is never in that
loop. The only thing tying the bot's uptime to Claude today is that it
happens to be running inside a Claude Code cloud container, and those
containers get reclaimed if the account's Claude usage runs out.

Moving the exact same code to a $5/month VPS (or a spare always-on home
computer) removes that dependency completely. This doc is the plain-English
version of `scripts/deploy/setup_vps.sh`.

## What you need

- **A VPS.** Cheapest reliable options: DigitalOcean, Linode, Vultr (all
  ~$4-6/month for a small box), or Oracle Cloud's free tier (genuinely
  free forever, slightly more setup). Pick Ubuntu as the OS image.
- **SSH access to it** (the provider gives you an IP address + root
  password or an SSH key when you create the box).
- **A GitHub personal access token** for this repo, so the VPS can push
  `paper_state/` updates the same way this session does. GitHub ->
  Settings -> Developer settings -> Personal access tokens -> generate one
  scoped to this repo (repo:write).

## Steps

1. **SSH into the VPS:**
   ```
   ssh root@<your-vps-ip>
   ```

2. **Run the setup script** (this repo's `scripts/deploy/setup_vps.sh`,
   copy it over first -- e.g. `scp` it, or `git clone` the repo manually
   and run it from inside):
   ```
   bash scripts/deploy/setup_vps.sh https://github.com/<owner>/<repo>.git claude/ai-trading-bot-research-yolqhm
   ```
   This installs Python, clones the repo, creates a lean virtual
   environment (just pandas/numpy/requests -- not the full LLM stack,
   since the live desk never uses `--llm-committee`), installs the
   watchdog as a **systemd service**, and starts it.

3. **Give it push credentials** (shown at the end of the script's output):
   ```
   git -C /opt/only- remote set-url origin https://<your-token>@github.com/<owner>/<repo>.git
   ```

4. **Done.** From here the VPS:
   - Starts the desk automatically on every boot (systemd `enable`)
   - Restarts automatically if anything crashes -- two independent
     layers: systemd's own `Restart=always`, plus `watchdog.sh`'s
     internal 5-minute self-heal loop (the same one already running in
     this Claude session)
   - Runs the nightly self-train (Q-table retrain on real rough/trending
     market days) with zero Claude involvement, same as today
   - Pushes `paper_state/` to git every 5 minutes, so the record is safe
     even if the VPS itself ever goes down

## Checking on it later

```
ssh root@<your-vps-ip>
sudo systemctl status only-bots-watchdog     # is it running?
sudo journalctl -u only-bots-watchdog -f     # live log
```

## What still needs Claude

Nothing about trading. Claude is only useful after this point for the
things a plain script can't do well: reading the journal and explaining
what happened in plain language, researching a new strategy idea,
writing a code change, or checking in on a Routine/reminder schedule.
The trading itself keeps running with this session closed, this
container reclaimed, or Claude usage at zero -- as long as the VPS is
paying its own $5/month bill.

## Honest limits

- This still isn't literally "impossible to go down" -- the VPS
  provider could have an outage, the box could run out of disk, etc.
  What it removes specifically is the Claude-usage dependency, which was
  the actual fragile link before.
- The paper account's realism ceiling doesn't change: this is still
  simulated money proving out the strategy, not a funded/live account.
  Moving it to a VPS doesn't change what the numbers mean, only how
  reliably it keeps producing them.
