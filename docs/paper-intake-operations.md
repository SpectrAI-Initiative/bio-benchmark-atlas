# Paper intake operations

The paper pipeline is intentionally inert until the repository owner configures both repository-scoped authentication systems and completes a live golden run.

## 1. Create the organization GitHub App

Create `biobench-atlas-bot` under `SpectrAI-Initiative` and install it only on `bio-benchmark-atlas`.

Repository permissions:

- Contents: Read and write
- Issues: Read and write
- Pull requests: Read and write
- Metadata: Read

Do not configure a webhook. Record the App ID and generate a private key. Add:

- repository variable `BIOBENCH_APP_ID`
- repository secret `BIOBENCH_APP_PRIVATE_KEY`

The workflow exchanges these long-lived credentials for a short-lived installation token with `actions/create-github-app-token`. The App can create branches and Ready PRs but the workflow contains no merge operation.

## 2. Configure OpenAI extraction

Add repository secret `OPENAI_API_KEY`. Add repository variables:

- `PAPER_EXTRACT_MODEL=gpt-5.6-sol`
- `PAPER_VERIFY_MODEL=gpt-5.6-sol`

The pinned Python dependency is `openai==2.46.0`. Model aliases may not be changed in production until the proposed combination passes the live golden workflow. Full-text sources are uploaded with Files API purpose `user_data`, both Responses use `store: false`, and deletion is attempted in a `finally` block.

## 3. Run and verify the live golden evaluation

Manually run `Paper extraction eval`. It must verify:

- LifeSciBench 750 / 136 / 62 and no invented binding count;
- BioMysteryBench 99 / 76 / 23 and five repeats;
- distinct SpatialBench 146 and 159 snapshots;
- Anthropic × BixBench as a relationship without a numeric result.

Only aggregate pass/fail metadata is retained. Production intake refuses to run when the latest success is older than 35 days.

## 4. Protect paper intake PRs

After the owner-gate workflow exists on `main`, add required status check `paper-owner-gate` alongside `validate` and `playwright`. Keep required approval count at zero for ordinary PRs: the custom check applies only to branches named `paper-intake/*` and accepts only a `wang422003` APPROVED review submitted after the latest head commit.

Do not enable auto-merge. If the bot pushes again, the commit timestamp becomes newer than the old approval and the gate fails until the owner re-approves.

## 5. Recovery

- `needs-human-review`: do not retry around refusals, parse failures, source conflicts, missing creator evidence, or unresolved identity/version/count claims.
- `extraction-failed`: retry only after a transient connection, 429, or 5xx failure is understood; the client itself performs at most three exponential-backoff attempts.
- stale candidate: a candidate closes after 60 days without approval. Reopen it and re-add `approved-for-intake` to start a new idempotent run.
- temporary OpenAI file cleanup warning: use the file ID in the private Actions log to delete the file manually before retrying.

Candidate issues, PDFs/full text, and short verification excerpts are never release assets or website data.

