# Road safety SQL agent

An agentic text-to-SQL application over the UK Department for Transport's
STATS19 road casualty database. Postgres, LangChain 1.x, layered query
guardrails, and an evaluation harness that scores by execution match.

Data licence: Open Government Licence v3.0.
Source: https://www.gov.uk/government/statistics/road-safety-data

## Run it

```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY
docker compose up -d          # Postgres on :5433, roles created on first boot

poetry install

poetry run crashquery download --from-year 2019 --to-year 2023
poetry run crashquery load --from-year 2019 --to-year 2023

poetry run crashquery check
poetry run crashquery ask "How many people were killed on the roads in 2022?"
poetry run crashquery chat
poetry run crashquery tui
```

`poetry run python -m crashquery` is equivalent to `poetry run crashquery`.

Roughly 700MB of CSV and a few minutes to load.

Optional extras:

```bash
poetry install --extras openai   # OpenAI models
poetry install --extras ml       # pandas / scikit-learn
```

## Why this dataset

STATS19 publishes coded integers, not labels. `casualty_severity = 1` means
Fatal, and nothing in the schema says so — the dictionary is a separate
spreadsheet. That gap is the whole point: it forces the agent to do metadata
retrieval rather than pattern-match column names, which is the failure mode
Spider 2.0 identified as what breaks text-to-SQL in production.

It also has three tables at three different grains and a genuine methodology
break in the middle of the series, so there are real ways to be confidently
wrong.

## Architecture

```
question
   ↓
create_agent (LangChain 1.x)
   ↓
tools ──── list_tables / describe_table     schema, with grain descriptions
      ├─── lookup_codes / find_coded_field  the code dictionary
      ├─── validate_sql                     parse + EXPLAIN, no execution
      ├─── run_sql                          guarded execution
      ├─── profile_column                   single-column EDA
      └─── build_ml_dataset                 join-correct modelling extract
   ↓
guard.py → sqlglot parse → EXPLAIN cost gate → psycopg
   ↓
Postgres as rsa_agent (read-only role, 30s statement timeout)
```

## The guardrails

Four layers, because any one of them will eventually be bypassed:

| Layer | Blocks | Failure mode if absent |
|---|---|---|
| `rsa_agent` role | all writes | the real security boundary |
| `statement_timeout` | runaway queries | one bad join pins the database |
| `guard.py` | non-SELECT, catalog reads, dangerous functions | model gets no usable error |
| EXPLAIN cost gate | expensive plans | 40s wait before an OOM |

`guard.py` is for good error messages, not for security. It's a parser and
parsers have gaps — two of which turned up while writing it:

- **`SELECT * INTO evil FROM collisions`** parses as a `Select`, so a
  root-node type check passes it. In Postgres it's `CREATE TABLE AS`.
- **`WITH d AS (DELETE FROM collisions RETURNING *) SELECT * FROM d`** is a
  data-modifying CTE and is also rooted at a `Select`.

Both are caught by walking every node in the tree rather than checking the
root. Neither could have caused damage anyway, because the role cannot write —
which is exactly why the role matters more than the parser.

Aggregate queries skip the automatic `LIMIT 200`. A `GROUP BY` can
legitimately return more than 200 groups; clipping them silently would break
evaluation. An explicit enormous `LIMIT` is still capped.

## Evaluation

```bash
poetry run crashquery eval
poetry run crashquery eval --only fatal_casualties_2022
```

Scoring is **execution match**: run the agent's SQL and the reference SQL,
compare result sets order-insensitively. Comparing SQL strings fails every
correct rewrite; using a judge model is slower, costlier and less reliable than
just running the query.

Cases also assert on **behaviour** via `must_call`. A question about fatal
casualties answered correctly *without* consulting the code dictionary was
answered by luck and will break on the next coded column. That distinction is
invisible to output-only scoring.

The gold set targets specific traps:

| Case | Trap |
|---|---|
| `fatal_casualties_2022` | fatalities live in `casualties`, and severity 1 = Fatal |
| `grain_trap_collisions_vs_casualties` | the mirror case, where `collisions` IS right |
| `missing_code_in_average` | `-1` is a missing-value code stored as a number |
| `motorcycle_casualties` | motorcycles span six separate `vehicle_type` codes |
| `severity_trend_artefact` | severity reporting changed mid-series |
| `counts_not_rates` | raw counts reflect exposure, not danger |

## Design notes

**Schema as a tool, not a prompt dump.** The built-in `SQLDatabaseToolkit`
pastes the schema into the system prompt. That costs context on every turn and
degrades as the schema grows. Making the model ask means it pays only for what
it needs.

**Prompt rules come from observed failures.** Every rule in `prompts.py`
corresponds to a specific wrong answer produced during development. Growing a
prompt from imagined failures produces bloat; growing it from real ones
produces a changelog.

**Load through staging with guarded casts.** COPY straight into typed columns
looks cleaner and aborts the whole file on one bad value after several minutes.
Staging as TEXT and casting with a regex guard never raises — bad values become
NULL and get counted, so you learn what you lost.

## Next steps

- Join ONS population estimates on LSOA so the agent can compute rates, not
  counts — the single biggest analytical gap right now
- Few-shot retrieval of similar past questions from pgvector
- Self-correction loop: feed `validate_sql` failures back before executing
- Run against Spider 2.0's Postgres slice for a comparable public number
