# Newsletter bullets eval: cases

Eight real newsletters from the `Newsletters/Skipped` label (Nov 2026), sanitized by `sanitize.py`
(tracking tokens stripped, recipient address replaced). Each case runs through the real
`summarize_message` and is graded on the expectations below. Reviewed by an independent Fable audit
(Nov 22, 2026); its fixes are applied. On Nov 23 the grader was tightened to per-case bullet budgets
(below) and a 30-word cap per bullet, matching the prompt's "one to three, usually one or two" bullets of about
20 words.

| id | tags | expected access | max bullets | publication | authors |
|---|---|---|---|---|---|
| `dense-essay` | dense-essay, full | full | 2 | Derek Thompson | ['Derek Thompson'] |
| `multi-story` | multi-story, full | full | 3 | Import AI | ['Jack Clark'] |
| `recap` | recap, full | full | 2 | Puck / The Backstory | ['Jon Kelly'] |
| `paywall-preview` | partial, paywall | partial | 2 | The Lowe Post | ['Zach Lowe'] |
| `partial-listicle` | partial, listicle | partial | 3 | Understanding AI | ['Kai Williams'] |
| `sponsor-heavy` | sponsor-heavy, full | full | 2 | Bytes | [] or ['Tyler'] |
| `single-idea` | single-idea, full | full | 2 | Tacklebox | ['Brian'] or [] |
| `thin-promo` | thin, promo | full | 1 | Slow Boring | ['Matthew Yglesias'] |

## Grading

**Code checks**, graded on the model's raw JSON before the app trims or replaces anything:
- `bullets`: 1 to `max bullets` (the case's `max_bullets`) non-empty bullets.
- `access`: `content_access` matches (`any` accepts either).
- `url`: the chosen link is one of the acceptable URLs.
- `direct`: no bullet describes the article instead of stating it ("this issue covers", "the author explores", "discusses").
- `concise`: every bullet is `MAX_WORDS_PER_BULLET` (30) words or fewer.
- `publication` / `authors`: reported, not part of pass.

**Judge** (`anthropic/claude-opus-5.5` via OpenRouter, structured output). It reads the email body, the link list the
model saw, the bullets, and the case key:
- `key_facts`: share of *must capture* points stated as the central claim plus at least one listed specific.
- `faithful`: 0 if a *must not claim* item fires or the judge finds a **material** unsupported claim, meaning the
  reader would believe something false. Minor slips (loose attribution, rounding) are counted separately as `minor_claims`.

**Headline `pass`**: the five gating code checks pass, `faithful` = 1, and `key_facts` >= 2/3.
With 8 cases and 2 runs each, one flip moves the score about 6 points. Differences under about 25 points are noise.
The judge is the same model as the app (the owner chose to keep it), so watch for self-preference on judge metrics.

The run summary prints pass rate (with a 95% Wilson interval), the mean of each metric, average bullets per
output, words per bullet, words per output, median latency, and total cost.

## Results (Nov 23, 2026)

Opus 5.5, 8 cases x 2 reps, about $1.27 per run, graded with the tightened grader. Results from the earlier,
looser grader (3 bullets for every case, 40 words) are archived in `.claude/hillclimb/newsletter-bullets-grader2/`
(and an earlier round in `-grader1/`); they are not comparable with these.

| Variant | Prompt | Pass | Avg bullets | Words/bullet | Words/output | Key facts | Faithful |
|---|---|---|---|---|---|---|---|
| baseline | before the Nov 23 change | 0/16 | 2.75 | 32.3 | 88.9 | 0.97 | 1.00 |
| v1 | shipped | 9/16 | 2.50 | 25.8 | 64.5 | 0.98 | 1.00 |
| v2 | "two by default, fold extras into 'Also:'", reverted | 8/16 | 2.50 | 26.8 | 67.0 | 0.95 | 1.00 |
| v3 | "a third bullet is rare", reverted | 8/16 | 2.50 | 26.2 | 65.5 | 0.95 | 1.00 |

The remaining failures are dense-essay, recap, and sponsor-heavy, where Opus keeps an "also"-tier third bullet
over the budget of 2 whatever the wording, plus thin-promo, whose single bullet sometimes runs past 30 words.

## `dense-essay`: Why Everything Is Suddenly Breaking All at Once

*Derek Thompson*, 2026-11-19. 6.3k-word interview essay; must pick the load-bearing arguments (in at most 2 bullets), not the anecdote it opens with.

**Must capture**
- The recent outages (FAA ground stop, bank withdrawal failures, United, NSA) were mostly self-inflicted: organizations rushing untested patches for AI-discovered vulnerabilities, not cyberattacks.
- AI is accelerating offense: about 3x more vulnerabilities found than last year or discovery-to-exploit time down to about a day, and push-button tools like ARTEX hand attack capability to unskilled actors.
- Pahlka: more money (e.g., the $50B air-traffic-control request) won't fix government tech, because the problem is outsourced expertise and contracting-driven procurement; government needs in-house technical expertise.

**Must not claim**
- That the outages were caused by a coordinated AI cyberattack.

**Acceptable URLs**
- `https://open.substack.com/pub/derekthompson/p/why-everything-is-suddenly-breaking-58c`
- `https://substack.com/app-link/post?publication_id=2880588&post_id=225684445`

## `multi-story`: Import AI 481: AI sub-contractors; a wet lab for AI; and subterfuge during development

*Jack Clark from Import AI*, 2026-11-16. Research briefing with five original write-ups plus a short story; the job is choosing the three that matter (the subject line names its own top three) and giving each one's finding.

**Must capture**
- FrontierML: AI agents can turn money into better ML results (e.g., Opus 5.5 gains 45.4 points with $10 and 58.8 with $200, versus a human researcher's 56 and 66.1).
- RSI Control Arena (Scale AI): frontier models can covertly complete sabotage side tasks while building their successor, and monitors miss many (e.g., 48% of attack runs achieve the side task, or monitors catch only 56-81% at a 2% false-positive rate).
- Petrichor, a YC wet lab that AI models can run experiments through via an API ('lights out' science); or LEMMO, a Chinese foundation model for electromagnetic signals including radar and communication jamming.

**Must not claim**
- Numbers attached to the wrong study or model (e.g., FrontierML figures presented as RSI Control Arena results).
- The Tech Tales fiction ('Digital Cornbread') presented as real events.

**Acceptable URLs**
- `https://open.substack.com/pub/importai/p/import-ai-481-ai-sub-contractors`
- `https://substack.com/app-link/post?publication_id=1317673&post_id=225143696`

## `recap`: Jensen Truthers & Netflix–NBCU Questions

*Jon Kelly*, 2026-11-21. Weekly roundup pointing to stories published elsewhere; must surface the lead stories as pointers without inventing detail from the linked articles. The owner values the holiday gift guide, so a bullet on it counts as a legitimate item.

**Must capture**
- Matt Belloni's story on a possible Netflix run at (bid for) NBCUniversal.
- Ian Krietzberg's story on the odds of Jensen Huang's return to Nvidia.
- At least one more item with the specifics the email gives: the Wachowskis reclaiming The Matrix; Christie's $627M evening sale; FIFA's World Cup rights auction falling flat; Ioffe puncturing the 'Yalta 2.0' hype around the Trump-Putin-Xi Shenzhen summit; or Puck's 2026 holiday gift guide (Mirth & Merriment) and its guest editors.

**Must not claim**
- Any detail of a linked story that is in neither the email body nor its link list (e.g., a bid price for NBCUniversal, or the reasons FIFA's auction is struggling).
- That Netflix has made or agreed to an offer for NBCUniversal (the email only says Belloni floated/eyed it).
- Presents a linked story's findings as if this email reported them in depth; bullets should read as pointers to who covered what.

**Acceptable URLs**
- `https://mail.google.com/mail/u/0/#inbox/eval-recap`

## `paywall-preview`: The Pistons Should Go for It

*Zach Lowe*, 2026-11-18. Paid preview that cuts off mid-argument at 'Keep reading with a 7-day free trial'; must be marked partial and must not guess the paywalled recommendation.

**Must capture**
- Lowe argues the Pistons should 'go for it' with a win-now move this season.
- His case: Cade Cunningham is ready now at 25, the East is weaker than expected, and the win that snapped San Antonio's 12-game streak was a proof of concept.
- The counterarguments he grants: unresolved Duren/Thompson spacing, expensive extensions on top of Cunningham's max, and last year's playoff run possibly owing to Franz Wagner's injury.

**Must not claim**
- Any specific trade target, trade package, or player the Pistons should acquire (that part is behind the paywall).

**Acceptable URLs**
- `https://zachlowe.substack.com/p/detroit-pistons-trade-rumors`
- `https://open.substack.com/pub/zachlowe/p/detroit-pistons-trade-rumors`
- `https://substack.com/app-link/post?publication_id=11524252&post_id=225579955`

## `partial-listicle`: 19 charts that explain the state of AI

*Understanding AI*, 2026-11-18. '19 charts' preview that shows charts 1-9 and cuts off at chart 10; a tempting title to over-claim from. Must be partial and must use only the visible charts.

**Must capture**
- Big Tech AI capex is historically unprecedented: $230B in Q3 2026 (+118% YoY) or about 2.3% of GDP.
- AI revenue is keeping pace: a $345B ecosystem run rate (4.2x YoY) or Anthropic's rise from $9B to $81B.
- Financing strain: Amazon, Google, Meta and Oracle now spend more on capex than operating cash flow and need outside capital.
- Business adoption is plateauing, per Ramp data.

**Must not claim**
- Content from charts 10-19, beyond the visible teaser that demand for AI chips is intense.
- That the email presents all 19 charts.

**Acceptable URLs**
- `https://www.understandingai.org/p/19-charts-that-explain-the-state`
- `https://open.substack.com/pub/understandingai/p/19-charts-that-explain-the-state`
- `https://substack.com/app-link/post?publication_id=1501429&post_id=225563317`

## `sponsor-heavy`: Bytes: Solid 2.0 is thriving

*Tyler from Fireship*, 2026-11-20. Short dev newsletter with a sponsor block and three [sponsored] items mixed into the links; ads must not become bullets.

**Must capture**
- Solid 2.0 shipped and SolidStart is sunset (server functions move into the core and/or Solid's Vite plugin handles dev server, SSR and builds).
- Development builds report errors with location and recommended fix, pointing agents to skills: observability for agents is now first-class.
- Next bets: ReactiveScript (a language that compiles to Solid) and/or Reactive Server Components (HTML diffs instead of JSON).

**Must not claim**
- Sponsored content as news (Sentry's metrics workshop, Expo Simulators, Greptile, Blacksmith).

**Acceptable URLs**
- `https://bytes.dev/archives/540`

## `single-idea`: No Whisper Ideas: Especially Earl

*Brian from Tacklebox*, 2026-11-22. A complete, short post with one idea; must say it in one or two bullets without padding or the program promo at the top.

**Must capture**
- Because AI has flooded inboxes with cold outreach, marketing copy now has to win on specificity: concrete detail builds trust and stands out (the 'Especially Earl' joke).
- Be specific about the customer's Momentum (what they already do to solve the problem) and Quirk (the constraint that makes common solutions useless), not about your solution.

**Must not claim**
- The Tacklebox Membership program or any promotion.
- A second bullet that restates the first or adds claims not in the email.

**Acceptable URLs**
- `https://redacted.invalid/link-7`

## `thin-promo`: Live podcast — and a discount!

*Matthew Yglesias*, 2026-11-20. Essentially an ad for a live podcast show; must collapse to a single bullet with the event details, no padding, and must not reveal or invent the paywalled code.

**Must capture**
- The first live edition of The Argument podcast (Yglesias and Jerusalem Demsas): Wednesday, December 9, Miracle Theatre, Washington, D.C.
- Paid Slow Boring subscribers get about 40% off tickets.

**Must not claim**
- A discount code, or podcast topics or guests not in the email.
- Background or marketing claims beyond the event details.

**Acceptable URLs**
- `https://open.substack.com/pub/matthewyglesias/p/live-podcast-and-a-discount`
- `https://substack.com/app-link/post?publication_id=159185&post_id=225727492`

## Running it

```bash
uv run python evals/newsletter_bullets/run_eval.py --reps 2                # -> baseline
uv run python evals/newsletter_bullets/run_eval.py --variant v4 --reps 2   # after a prompt change
uv run python evals/newsletter_bullets/run_eval.py --cases recap --reps 2  # a subset
```

Needs `OPENROUTER_API_KEY` (or the Keychain entry in the config). It uses `summary.prompt` and `summary.model`
from `config.example.yaml` unless `--config` (or `--model`) says otherwise; the bullet rules come from
`openrouter.py`. `--reps` defaults to 1, `--concurrency` to 6. Results land in
`.claude/hillclimb/newsletter-bullets/<variant>/` (`results.jsonl`, `traces/`, `errors.jsonl`); baseline through v3
are taken, so the next variant is v4. Re-running resumes, skipping (case, rep) pairs already recorded, so move the
directory aside before re-grading after a grader or case change. A full pass is 8 cases x 2 runs on Opus 5.5 plus
the judge: about $1.27 and under a minute. To refresh the fixtures, re-export from
the production container (read-only) and run `sanitize.py`. It refuses to write a fixture that still identifies the recipient.
