# Cyber Brief

Checks cybersecurity RSS feeds twice a day and posts a digest of anything new to
Discord. It can also rate each digest by severity and send an alert. Already-sent
items are remembered in `news.db`, so you never get repeats.

## Files

```
news_updater.py      the bot
severity.py          scores stories against your keyword lists
config/              the text files you edit
  severity_keywords.example.txt   keyword lists and rules (copy to severity_keywords.txt)
  notify_messages.txt             alert messages for each severity level
.env.example         settings for running it on your own computer
.github/workflows/   the GitHub Actions schedule
```

## Setup

```
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python news_updater.py --dry-run   # prints the digest, sends nothing
python news_updater.py             # sends for real
```

Fill in `DISCORD_WEBHOOK_URL` with your Discord webhook URL.

## Discord forum channels

If you want each digest as its own post, create the webhook inside a forum
channel and set `DISCORD_FORUM=true` in `.env`. Posts are titled by time of day,
for example "October 26 Morning Digest" (before noon), "Afternoon Digest"
(noon to 3 PM), or "Evening Digest" (3 PM on), and the stories go in the post
body with a short summary under each link and the author's name when the feed
provides one. Long digests continue inside the same post, and each source
heading appears only once. For a regular text channel, leave
`DISCORD_FORUM=false`.

## Get a notification for each digest (optional)

Discord only sends a notification when a message mentions you. To get one for
every digest, set `DISCORD_PING_USER_IDS` to your Discord user ID (turn on
Developer Mode in Discord's Advanced settings, then right-click your name and
choose Copy User ID). Separate several IDs with commas. The bot can only ever
mention the IDs you list: it never pings roles, `@everyone`, or `@here`.

## Severity alerts (optional)

To get an alert in a separate channel, with a ping, whenever a digest is posted:

1. Make a normal text channel (not a forum), for example `new-post-notification`,
   and create a webhook in it. This is a second webhook, separate from the digest.
2. Make a role, turn on **Allow anyone to @mention this role** in its settings,
   and give yourself the role. Copy the role ID (Developer Mode, then right-click
   the role in Server Settings > Roles).
3. Set `DISCORD_NOTIFY_WEBHOOK_URL` and `DISCORD_NOTIFY_ROLE_ID`. Optionally set
   `DISCORD_GUILD_ID` (right-click the server icon > Copy Server ID) so the alert
   links to the digest post.

The alert uses a message from `config/notify_messages.txt` for the digest's level.
Only that one role is ever pinged. Manual runs with no new stories send a "usual"
alert so you can test it. Set `DISCORD_NOTIFY_ON_EMPTY=true` to get that on
scheduled runs too.

## Severity levels (optional)

The bot can rate each digest as **severe**, **notable**, or **business as
usual** by scanning every story's headline and summary against a keyword list.

1. In the `config` folder, copy `severity_keywords.example.txt` to
   `severity_keywords.txt` and edit your copy. (If there is no
   `severity_keywords.txt`, the example file is used.) Commit your copy to your own
   private repo so the scheduled runs use it.
2. Add the software you depend on to the `[watch1]` list.

How the lists work together:

- `[tier1]`: extreme alarms such as "zero-day" or "actively exploited". One hit
  means **severe**.
- `[tier2]`: a broad list of important words. One hit means **notable**. Next to
  a `[watch1]` name it becomes **severe**.
- `[tier3]`: weak words such as "attack" or "patch". They only count next to a
  watched name.
- `[watch1]` is your own software, `[watch2]` is popular software worth a look,
  and `[change]` holds admin-change words such as "retire" or "end of support".
- The `[rules]` section at the bottom of the keyword file spells out exactly how
  these combine, and you can edit it.
- A digest is as severe as its most severe story.

Matching is not case sensitive and treats hyphens as spaces, so "zero-day" and
"zero day" are the same. A plain term also matches longer forms (`exploit`
matches "exploited"), `=term` matches the whole word only, and `re:pattern` is a
regular expression.

The short messages used for the alert live in `config/notify_messages.txt`, in
`[severe]`, `[notable]` and `[usual]` sections with one message per line. The bot
picks one at random for the digest's level.

## Run every 12 hours

**Mac/Linux (cron):** run `crontab -e` and add:

```
0 9,17 * * * cd /path/to/cyber-brief && /usr/bin/python3 news_updater.py >> run.log 2>&1
```

**Windows:** Task Scheduler > Create Basic Task > repeat every 12 hours >
action: `python news_updater.py`, start in the project folder.

Note: your computer has to be on at those times. For always-on delivery, run
it on a free-tier VM or a Raspberry Pi, or use a GitHub Actions schedule.

## Run it in the cloud with GitHub Actions (computer can be off)

1. Click **Use this template > Create a new repository** and choose **Private**.
   (Or create a private repo yourself and upload all the files, including
   `.github/workflows/digest.yml`.)
2. Go to **Settings > Secrets and variables > Actions > New repository secret**
   and add `DISCORD_WEBHOOK_URL` (your webhook URL). Optional secrets:
   `DISCORD_TAG_IDS`, `DISCORD_PING_USER_IDS`, `DISCORD_NOTIFY_WEBHOOK_URL`,
   `DISCORD_NOTIFY_ROLE_ID`, `DISCORD_GUILD_ID`.
3. Open `.github/workflows/digest.yml` and set `TIMEZONE`, the `cron` hours and
   the time window to your own time zone (see the note below).
4. Go to **Settings > Actions > General > Workflow permissions** and make sure
   "Read and write permissions" is selected.
5. Open the **Actions** tab, choose "Cyber news digest", and click **Run
   workflow** to test it.

As shipped, it runs at **8:55 AM and 4:55 PM Denver time**, so the digest is
posted by 9 and 5. To change that, edit `TIMEZONE`, the `cron` hours, and the time
window in the "Check local time" step of `digest.yml`. GitHub sometimes starts
scheduled runs a few minutes late, and the time window allows for that. The list
of links it has already sent is saved in a file called `news.db` on a branch named
`state`, so your main branch stays clean.

GitHub's scheduler can also skip or badly delay runs. If that happens, start the
workflow from a free outside timer such as cron-job.org instead: it sends a POST
request to GitHub's `workflow_dispatch` API at the times you choose, using a
fine-grained token with Actions read and write access to this repo.

If your channel is a regular text channel instead of a forum, change
`DISCORD_FORUM: "true"` to `"false"` in `digest.yml`.

## Customizing

- Add or remove sources in the `FEEDS` dict at the top of `news_updater.py`.
- `MAX_IN_DIGEST` caps how long each digest gets.
