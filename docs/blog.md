*How I built Steward Crew on Switch: three AI agents in Discord and Slack, one plain-Python Ledger, and a maintainer who types `approve 7`.*

Hi, I'm Jahaan Suthar, a computer science student from Chennai. Every time I open an open-source project, I see maintainers buried in comments and claims instead of writing code. So for the Flip the Switch hackathon, I built them a crew.

It is Monday morning on a small open-source project. Overnight, fourteen people commented "can I work on this?". Three of them want the same issue. One account, two days old, claimed four issues and opened a 600-line pull request that rewrites the README with rocket emoji. Someone who claimed the Windows docs last week has gone quiet.

None of this is code. All of it lands on the maintainer before they can write any. I wanted an AI crew to take that work off them, without ever letting an AI do something on GitHub that a person did not approve. The rule I built everything around fits on one line:

> AI proposes, the Ledger acts, a maintainer decides.

Here it is working, in under three minutes:

{% youtube yIe22a9f0eM %}

## What I built

Steward Crew is three AI agents and one program that is not AI, working across Discord and Slack through Switch.

| Who | Where | What it does |
| --- | --- | --- |
| Stu (AI) | Discord `#help`, public | Answers newcomers from the real code, finds them a free issue, and files the request to assign it. Says it is an AI. |
| Steward (AI) | Slack `#maintainers`, private | Every morning: who claimed what, which claims went quiet, which issues are contested, which pull requests look off. One proposal per decision. |
| Reviewer (AI) | Slack `#maintainers` | Reads a flagged pull request against its issue and the code, and drafts a review or a polite close. |
| Ledger (not AI) | Slack `#maintainers` | The only part with a GitHub token. Posts each proposal as a numbered card and acts only when a maintainer types `approve 7`. |

![Only a maintainer's approval reaches GitHub: the three AI agents file proposals, the Ledger posts a numbered card, and only a maintainer's approve makes it act on GitHub](https://raw.githubusercontent.com/TheRealJahaan/steward-crew/main/docs/how-it-works.png)

The AI agents can only file proposals. The Ledger turns each one into a card, and only a maintainer's `approve` makes it act on GitHub.

On a test run with a busy week of made-up contributors, Steward's morning summary filed ten cards in under two minutes. It assigned three new claims and gave a contested issue to the person who asked first, with a kind note to the other pointing at free issues. It released two claims that had been quiet for 70 and 96 hours, and drafted a one-claim-rule note for an account holding four. It handed the two suspicious pull requests to the Reviewer. I approved four cards with one line: `approve 1 4 5 7`.

## How I set it up on Switch

This is the part I wish I had read on day one. Everything below ran on a MacBook Air with Switch Console's local server (v0.40.1).

### 1. Connect Discord

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application, add a bot, and copy its token.
2. On the Bot page, turn on the two privileged intents: **Server Members** and **Message Content**. Save. Without them the bridge says it is not running.
3. In Switch Console, connect Discord with the bot token, the **Application ID** as the bot ID, and your server's ID as the **Guild ID**. To get it: Discord settings, Advanced, Developer Mode on, then right-click the server icon and choose Copy Server ID.
4. Post any message in `#help`. Switch turns a channel into a room the first time someone talks in it.

Two errors I hit: `Unknown Guild (10004)` means the Guild ID is wrong, and `LoginFailure` means the token is. To change the token, use **Disconnect app** and connect again.

### 2. Connect Slack

Install the Switch Slack app, then in your private `#maintainers` channel type `/invite @Agent Switch`. That channel is now a room. Do it in the channel you mean: my first room was `#all-steward-maintainers` by accident.

### 3. Describe the crew as a template

A Switch template is one YAML file: the rooms it needs as parameters, and the agents with their instructions, tools and rooms. Mine, trimmed:

```yaml
version: 1
params:
  help_room:
    type: room
    label: "Community room (Discord #help)"
    default: [help, "#help"]
  maintainers_room:
    type: room
    label: "Maintainers room (Slack #maintainers)"
agents:
  - name: stu
    display_name: Stu (AI)
    directory: "{$agents_dir}/{agent}"
    join: ["{help_room}"]
    advanced_config:
      tools: [Read, Grep, Glob, Bash]
      disallowedTools: ["Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]
      maxTurns: 25
    instructions: |
      You are Stu, the AI helper for an open-source project...
```

In Switch Console: **Templates**, **Import template**, choose the file, then **Use template** and **Create agents**. Switch creates the agents, gives each its own folder, and puts them in the rooms.

Quote any label that contains `#`. In YAML an unquoted `#` starts a comment, and my labels came out cut off.

### 4. Add a non-AI agent: the Ledger

Switch agents do not have to be AI. The Ledger is a small Python program that registers with the local Switch server as an always-on agent and opens a WebSocket to its rooms. It reads every message in `#maintainers`, not only the ones that name it. In Slack, `!invite-agent @ledger` puts it in the room.

### 5. Three room commands worth knowing

| Command | What it does |
| --- | --- |
| `!invite-agent @ledger` | Adds an agent to the room |
| `!reset @stu` | Starts the agent's session fresh, after you change its settings |
| `!follow-ups off` | Agents answer only when someone @-mentions them |

I needed `!follow-ups off` in the maintainers room. By default, Switch sends your next message without an @ to the agent you just talked to. So my `approve 11 12` reached the Reviewer, which tried to be helpful about it.

## Why it is safe to let AI near your contributors

A prompt that says "never post on GitHub" is a wish, not a guarantee. Every rule here is enforced by code, by Claude Code, or by Switch itself.

**Only the Ledger holds a GitHub token.** It is a fine-grained token for one repository, kept in the macOS Keychain. The agents never see it. The Ledger also keeps a copy of the code fresh for the agents to read, passing the token to git one command at a time, so there is nothing in `.git/config` to borrow.

**The agents' tools are locked down.** In the template they get Read, Grep, Glob and Bash, and no editing or web tools. `steward-ledger crew` then writes Claude Code permission rules into each agent's folder: the `steward` command is allowed, and `gh`, `git push`, `curl`, the Keychain and the Ledger's files are denied.

**Rules are code, not prompts.** "One open claim per newcomer" and "a claim is quiet after 48 hours" live in `policy.yaml` and are computed in plain Python. The spam checks on a pull request (no linked issue, no tests, boilerplate phrases) are too. The AI explains and drafts. It does not decide what the rules are.

**Every action is a numbered card.** The card says what will happen, why, and the exact text that will be posted. Maintainers answer with `approve 7`, `decline 7 <why>` or `edit 7: <new text>`. Everything is logged, and `undo 7` deletes the comment and removes the assignment.

**An AI cannot approve.** The Ledger ignores messages from agents. Switch backs this up: when the Reviewer once tried "@ledger approve 11 12", Switch's addressing rules refused it before the Ledger ever saw it.

**The demo data is fenced off.** `steward demo flood` plays a scripted busy week of made-up contributors. Any card that touches one of them, even a comment that only names one, is marked "Demo data: approving it is simulated" and never reaches GitHub, where those usernames may belong to real people.

**It says it is AI.** Stu introduces itself as the project's AI helper, and every GitHub comment ends with "Drafted by Steward Crew (AI) and approved by a maintainer."

## What went wrong, and what I learned

**A tool rule that switched off the whole tool.** I first put `Bash(gh:*)` and `Bash(curl:*)` in each agent's disallowed tools, to forbid just those commands. Instead the agents reported "Bash is disabled" and could not run `steward` at all. The fix: in the agent definition, list only whole tools. Put command-level rules in `.claude/settings.json` in the agent's folder, which Claude Code reads when Switch starts it. Then `!reset` each agent so it picks them up.

**When an agent ignores its brief, make the command refuse.** Steward's brief says to hand suspicious pull requests to the Reviewer. On the first busy-day run it did that, and it also filed two review cards whose text was just the raw check result. Rewriting the prompt would have been a wish. Instead, `steward propose review` now refuses unless the Reviewer files it, and the error says what to do: "@reviewer please look at PR #n". The next run went straight to the Reviewer. Agents read error messages well, so put the rule in the error.

**A safety gap I found before it cost anything.** My first rule for demo data checked the card's issue and person. A comment on a real issue that thanked "@asha-dev" passed as real, and approving it would have pinged whoever owns that name on GitHub. Now a card counts as demo if any name in it, or any demo activity on its issue, comes from the replay. The mark is saved when the card is filed, so clearing the demo later cannot turn a demo card real.

**The moment it clicked.** I asked the Reviewer about a made-up pull request that "fixes" case-insensitive search. It read the real code and found that the change runs the search text as a regular expression, so searching for "call (bank)" would crash the program. It suggested the one-line safe version, `text.lower() in t.title.lower()`. I had written that pull request as a test case. The Reviewer found its bug on its own, with an input that breaks it and the fix, and it was not in any rule I wrote.

**What I learned about Switch.** Rooms make Discord and Slack one world: the same agents and rules work in a public community channel and a private maintainers channel. A plain program can be an agent, which is how the one part with real power ended up with no AI in it. Templates make the whole crew reproducible from one file. And Switch's addressing and follow-up rules are not just convenience: they decide who can make an agent act.

I built Steward Crew with Claude Code as my pair programmer. It wrote most of the code from my design, and I tested every step on my own Mac.

## Try it yourself

You need macOS, Switch Console with its local server running, a Discord server and a Slack workspace connected to Switch, Claude Code signed in, and [uv](https://docs.astral.sh/uv/).

```console
$ git clone https://github.com/TheRealJahaan/steward-crew
$ cd steward-crew && ./scripts/setup.sh
$ ~/.steward/bin/steward-ledger run
```

Then import `switch/steward-crew.template.yaml` in Switch Console, run `~/.steward/bin/steward-ledger crew` to lock down the agents' folders, and say hi to Stu in `#help`. To see a busy day without a busy project, run `~/.steward/bin/steward demo flood`.

- Code: [github.com/TheRealJahaan/steward-crew](https://github.com/TheRealJahaan/steward-crew)
- The project it looks after: [tinytask](https://github.com/TheRealJahaan/steward-demo)
- Demo video: [watch on YouTube](https://youtu.be/yIe22a9f0eM)
- Switch: [github.com/sandbox-quantum/switch](https://github.com/sandbox-quantum/switch)

If you maintain a project that drowns in "can I work on this?", I would love to hear whether this would help you. Built for the Flip the Switch hackathon by WeMakeDevs and Switch. #HowISwitch
