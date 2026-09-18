# data-curator

[![CI](https://github.com/eirasroger/data-curator/actions/workflows/ci.yml/badge.svg)](https://github.com/eirasroger/data-curator/actions/workflows/ci.yml)

Decides what to do when someone proposes changing a published environmental
product record: apply it, reject it, or send it to a person.

Most of that decision is ordinary arithmetic. An LLM handles what the
arithmetic cannot settle. Fixed rules limit what its answer is allowed to
cause.

**[See the demo →](https://eirasroger.github.io/data-curator/)**

## Why

An Environmental Product Declaration is a published document. Other people
quote its numbers in their own calculations.

So changes are risky. Apply a bad one and a wrong number spreads, and you
cannot take it back. Apply nothing and errors stay in the data.

Changes arrive from three places. A reviewer spots an error, a manufacturer
publishes a new version, or a document reaches its expiry date. All three
become the same kind of request and take the same path.

## Built with

Runtime, in the order a request meets it.

| | |
| --- | --- |
| **FastAPI** | The four services. Small and quick to start, which matters on a platform that scales to zero |
| **Pydantic** | Every record, message and LLM answer has a declared shape. The LLM is held to a schema, so a malformed answer fails at the door instead of downstream |
| **Pub/Sub** | The queue between the front door and the worker. The front door answers in milliseconds, the slow work happens behind it, and a message that keeps failing lands in a dead-letter queue instead of looping |
| **OpenAI `gpt-5-mini`** | The judgement call. Behind a five-line interface, so swapping it is one class |
| **BigQuery** | The datastore in the cloud. Append-only history with SQL over it, no server to run |
| **DuckDB** | The same datastore on a laptop. One file, no account. This is what lets anyone run the project |
| **Jinja and HTMX** | The dashboard. Server-rendered, no build step, no JavaScript bundle |

Around it.

| | |
| --- | --- |
| **Cloud Run** | Hosting. Scales to zero, so an idle deployment costs nothing |
| **Secret Manager** | The API key and the webhook signing keys. Never in the image or the repo |
| **Cloud Scheduler** | Triggers the nightly job |
| **Terraform** | Every cloud resource above, described in one place and removable with one command |
| **Docker** | One image per service, all built from the repository root so they share the rules |

## How it works

![The decision pipeline](docs/img/pipeline.svg)

**`screen()` does the arithmetic.** EPD records check themselves. Density
times thickness should equal the stated weight per unit. The parts of a carbon
figure should add up to the total. Material percentages should reach 100%.

The proposed change is applied to a copy, and the record is checked again. A
change that breaks the arithmetic is rejected on the spot. No LLM involved.
Most changes stop here.

**`triage()` calls the LLM.** Only for what survived the arithmetic. It gets
one question. Is this a correction, or a mistake? It answers with a category, a
confidence score from 0 to 1, and a reason. OpenAI's `gpt-5-mini` by default,
and swapping it is one class.

**`finalise()` applies fixed rules to that answer.** Three of them, in
order.

1. A change to a published figure goes to a person, at any confidence.
2. A verdict of "implausible" rejects the change.
3. Confidence below the threshold goes to a person.

Anything that clears all three is applied. So a confident answer on an ordinary
field does decide the outcome. What the rules guarantee is the ceiling on that
authority. The LLM can never apply a change to a published figure, and never
applies anything it is unsure about.

These rules live in `domain/changes.py` as code, so they can be read and
tested. The prompt contains none of them.

## Running it locally

Runs the whole pipeline on your machine, in offline mode. A local database
file takes the place of BigQuery, and a small rule-based scorer takes the place
of the LLM.

```bash
git clone https://github.com/eirasroger/data-curator.git
cd data-curator
pip install -e ".[dev]"

python scripts/seed_local.py          # load the 63 records
python -m pytest                      # the test suite
python eval/run_eval.py               # score it against labelled cases
python sim/run.py --db local.duckdb --count 500
python scripts/dashboard.py --db local.duckdb
```

![The operator dashboard](docs/img/dashboard.jpg)

`sim/run.py` invents change proposals and pushes them through the same code the
deployed worker runs. `scripts/dashboard.py` then shows what it decided and
what is waiting for a person.

Add `--provider openai` to use the real LLM. That needs an API key in `.env`
and costs a fraction of a cent per call.

## Repository layout

| Path | Contents |
| --- | --- |
| `domain/` | The rules, the LLM interface, the database layer |
| `services/` | Four services: api, webhook, worker, dashboard |
| `sim/` | Generates proposals and runs them through the pipeline |
| `eval/` | Scores decisions against labelled cases |
| `analysis/` | Turns a run into charts and figures |
| `infra/` | Terraform for the Google Cloud resources |
| `sql/` | Table definitions for BigQuery and DuckDB |
| `seed/epds.json` | The 63 records |

The records come from a separate project that reads EPD PDFs. This one only
consumes them.

## Design notes

**Two databases, one interface.** `domain/store.py` defines what a datastore
has to do. BigQuery implements it in the cloud, DuckDB on your laptop. Same
methods, same tests. That is why the project runs without a cloud account.

**Nothing is edited in place.** Applying a change writes a new version and
leaves the old one alone. When a person overrules the system, both answers stay
on the record.

**Expiry is an event.** When a document passes its date, the nightly job files
a change request, the same way a person would. So there is always a record of
when it was noticed and what happened next.

**The public endpoint holds almost no permissions.** Manufacturers post signed
updates to a service of its own. It can publish to one queue and read its own
signing key. It has no access to the database.

**The tests run against the real records.** Rules that only meet invented data
tend to work only on invented data.

## Deploying to Google Cloud

```bash
terraform -chdir=infra apply       # dataset, tables, queues, identities, secrets
gcloud secrets versions add openai-api-key --data-file=-
python scripts/seed_bigquery.py
bash scripts/deploy.sh
```

The nightly job is created paused. `bash scripts/teardown.sh` removes the
services and stops the spending; data and secrets stay.

Google Cloud has no hard spending limit, so set a budget alert first.

---

# Analysis of a test run

The threshold for acting without a person was picked by hand. This is the work
that checked whether it was right.

2000 change proposals against the 63 real records, decided by the
real pipeline using `gpt-5-mini`, spread over 90 simulated days. 948 of them
reached the LLM, at a total cost of $0.87.

Nobody submitted those 2000 proposals. They were generated by breaking a value
and proposing the original back, which means the correct answer for each one is
known in advance. That is what makes the rest measurable.

Regenerate everything below with `python analysis/report.py`.

## The threshold was too low

It was set at 0.85. At that level, five wrong changes get applied with nobody
looking. At 0.90, none do.

![What each threshold would do](analysis/figures/floors.png)

| threshold | applied correctly | applied wrongly |
| --- | --- | --- |
| 0.95 | 6 | 0 |
| **0.90** | **134** | **0** |
| 0.85 (old) | 208 | **5** |
| 0.80 | 243 | 13 |
| 0.70 | 261 | 44 |

74 fewer changes applied automatically, in exchange for five published figures
not being corrupted. It is 0.90 now.

An earlier 150-case check had reported 0.85 as safe. The failure rate in that
band is around 2%, and 150 cases is too few to catch it. Small test sets find
regressions; setting a threshold needs a large one.

## The confidence score is worth trusting

![Stated confidence against how often it was right](analysis/figures/calibration.png)

| LLM says | actually correct |
| --- | --- |
| 0.25 | 21% |
| 0.60 | 26% |
| 0.75 | 37% |
| 0.83 | 81% |
| 0.88 | 94% |
| 0.93 | 100% |

It rises the whole way, with a sharp jump between 0.75 and 0.83. Below that the
LLM understates itself, which is the safe direction for a threshold that only
reads the top of the range.

## The rules do half the work for free

![What decided each outcome](analysis/figures/rules_vs_model.png)

1052 of the 2000 were settled by arithmetic alone. The remaining 948 LLM calls
cost $0.87 in total, about $0.0009 each, at a median of 4.4 seconds.

Two checks account for nearly every rejection. Density against thickness and
declared weight fired 166 times, carbon components against the declared total
165 times.
A further 46 were replacements that did not match the schema. The remaining
checks never blocked anything.

## The drift alarm works

![Daily rejection rate](analysis/figures/drift.png)

Normally the daily rejection rate sits between 0.30 and 0.50. When the mix of
incoming proposals is deliberately worsened, it climbs to 0.60–0.85 and the
nightly job raises a flag.

A second alarm watches mean confidence and fires below 0.60. It was going off
on normal traffic, but only in offline mode, which averages 0.53. The LLM
averages 0.77, well clear of the alarm. Left as it is.

## Offline mode has limits

Offline mode scores a proposal with a few arithmetic patterns. Run the pipeline
that way and the results describe those patterns. They say nothing about an LLM.

| Same 2000 proposals | offline | gpt-5-mini |
| --- | --- | --- |
| Applied | 349 | 565 |
| Rejected | 637 | 873 |
| Sent to a person | 948 | 562 |
| Mean confidence | 0.53 | 0.77 |
| Applied at a safe threshold | 0 | 134 |

Offline mode never rejects on judgement and never clears the threshold, so
everything it sees turns into review work. It exists so the pipeline can be run
and tested for free.

## One score got worse when the system got safer

The 54-case eval scored 53/54 at the old threshold and 51/54 at the new one.
Two correct changes now wait for a person.

The eval contains no example of a change that is wrong at 0.87, because 54
cases cannot contain one. The 2000-proposal run contained five.
