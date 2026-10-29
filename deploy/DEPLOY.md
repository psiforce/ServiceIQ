# Deploying the Phase 2–3 additions to Google Cloud Run

This adds three things to the existing ServiceIQ deployment (API, chat and admin on Cloud Run, Cloud SQL
Postgres, Secret Manager):

| Piece | Cloud Run | Image role | Reaches |
|---|---|---|---|
| Field-service platform | service `serviceiq-fsp`, **private** | `fsp` | Cloud SQL database `fieldservice` |
| Contractor onboarding | job `serviceiq-fsp-seed` | `fsp-seed` | same |
| Hourly sweep (approval timeouts; Phase 4: missed completions, payment and review reminders) | job `serviceiq-sweep`, run hourly by Cloud Scheduler | `sweep` | ServiceIQ database, `serviceiq-fsp` |

All of them use the same image as the rest of ServiceIQ. Replace `PROJECT`, `REGION`, `INSTANCE` and `IMAGE`
below (the values used in M13b: region `us-central1`, instance `serviceiq-db`).

## 1. Database and secrets

The platform gets its own database and a **normal role**. Superusers bypass row-level security, which is what
keeps each contractor's data separate inside one database.

```bash
gcloud sql databases create fieldservice --instance=INSTANCE --project=PROJECT
python deploy/gcp_secrets.py --project PROJECT        # adds fsp-database-url and fsp-api-keys
gcloud sql users create fsp_app --instance=INSTANCE --project=PROJECT \
  --password="$(cat var/gcp/fsp_db_password)"
# the services' account must be able to read the two new secrets
for s in fsp-database-url fsp-api-keys; do
  gcloud secrets add-iam-policy-binding $s --project=PROJECT \
    --member=serviceAccount:RUN_SERVICE_ACCOUNT --role=roles/secretmanager.secretAccessor
done
```

On Cloud SQL, users created with `gcloud sql users create` can create tables in a new database and do not
bypass row-level security, so no extra grants were needed (checked on Postgres 17: the platform logs no
warning at start-up). On a self-managed Postgres 15+, run `GRANT ALL ON SCHEMA public TO fsp_app;` in
`fieldservice` first.

In every `gcloud run deploy` and `gcloud run jobs create` below, add
`--service-account=RUN_SERVICE_ACCOUNT` (the account the other ServiceIQ services use; `serviceiq-run@…` in
M13b).

## 2. Field-service platform (private service) and onboarding job

```bash
gcloud run deploy serviceiq-fsp --image=IMAGE --args=fsp --region=REGION --project=PROJECT \
  --no-allow-unauthenticated --add-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-secrets=FSP_DATABASE_URL=fsp-database-url:latest

gcloud run jobs create serviceiq-fsp-seed --image=IMAGE --args=fsp-seed --region=REGION --project=PROJECT \
  --set-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-secrets=FSP_DATABASE_URL=fsp-database-url:latest,FSP_API_KEYS=fsp-api-keys:latest
gcloud run jobs execute serviceiq-fsp-seed --region=REGION --project=PROJECT --wait
```

The seed job creates the tables and row-level security, then onboards every
`data/tenants/*/fieldservice.json`. To add a contractor later, add its config and run the job again (or run
`python -m fieldservice add-org --config <file>` against the same database).

The service logs a warning at start-up if it is connected as a role that bypasses row-level security.

## 3. Point the API and admin console at the platform

The API and admin services call the private platform with a Google ID token.

```bash
FSP_URL=$(gcloud run services describe serviceiq-fsp --region=REGION --project=PROJECT --format='value(status.url)')
for svc in serviceiq-api serviceiq-admin; do
  gcloud run services update $svc --region=REGION --project=PROJECT \
    --update-env-vars=FSP_URL=$FSP_URL,FSP_ID_TOKEN=true \
    --update-secrets=FSP_API_KEYS=fsp-api-keys:latest
done
# allow their service account(s) to call the platform
gcloud run services add-iam-policy-binding serviceiq-fsp --region=REGION --project=PROJECT \
  --member=serviceAccount:API_SERVICE_ACCOUNT --role=roles/run.invoker
```

Booking is on by default (`AGENT_BOOKING_ENABLED=true`). Set it to `false` on the API to stay in Phase 2
(the office books every request), or use `AGENT_TOOL_ALLOWLIST` to turn booking off for one contractor.

## 4. Approval timeouts: hourly job

```bash
gcloud services enable cloudscheduler.googleapis.com --project=PROJECT   # off by default
gcloud run jobs create serviceiq-sweep --image=IMAGE --args=sweep --region=REGION --project=PROJECT \
  --set-cloudsql-instances=PROJECT:REGION:INSTANCE \
  --set-env-vars=FSP_URL=$FSP_URL,FSP_ID_TOKEN=true \
  --set-secrets=DATABASE_URL=database-url:latest,FSP_API_KEYS=fsp-api-keys:latest,OPENAI_API_KEY=openai-api-key:latest,PINECONE_API_KEY=pinecone-api-key:latest

gcloud scheduler jobs create http serviceiq-sweep-hourly --location=REGION --project=PROJECT \
  --schedule="0 * * * *" --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/PROJECT/locations/REGION/jobs/serviceiq-sweep:run" \
  --oauth-service-account-email=SCHEDULER_SERVICE_ACCOUNT
```

The OpenAI and Pinecone keys are only there because the settings loader requires them; the sweep makes no
model calls. The scheduler's service account needs `roles/run.invoker` on the job.

## 5. Check

```bash
gcloud run jobs execute serviceiq-sweep --region=REGION --project=PROJECT --wait   # "0 approval(s) escalated."
```

Then, in the deployed chat, request a visit with "can I get a discount?": it should wait in the admin console's
Approvals tab. A visit without one should be offered three times and book into the Schedule tab.

The admin console is private (no invoker binding, as in M13b). Open it with
`gcloud run services proxy serviceiq-admin --region=REGION --project=PROJECT` and browse to the local port.

## 6. Phase 4: invoice, payment and review

The agent invoices a job when it is completed, takes payment on Stripe's hosted page, and asks for a review.
Use **Stripe test keys** until the release gates in the Phase 4 document (section 5.9) are met.

```bash
# STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET in .env first (Stripe dashboard, test mode)
python deploy/gcp_secrets.py --project PROJECT   # adds fsp-webhook-secret, stripe-secret-key, stripe-webhook-secret
for s in fsp-webhook-secret stripe-secret-key stripe-webhook-secret; do
  gcloud secrets add-iam-policy-binding $s --project=PROJECT \
    --member=serviceAccount:RUN_SERVICE_ACCOUNT --role=roles/secretmanager.secretAccessor
done
API_URL=$(gcloud run services describe serviceiq-api --region=REGION --project=PROJECT --format='value(status.url)')
CHAT_URL=$(gcloud run services describe serviceiq-chat --region=REGION --project=PROJECT --format='value(status.url)')
gcloud run services update serviceiq-api --region=REGION --project=PROJECT \
  --update-env-vars=PUBLIC_BASE_URL=$CHAT_URL \
  --update-secrets=FSP_WEBHOOK_SECRET=fsp-webhook-secret:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest
gcloud run services update serviceiq-admin --region=REGION --project=PROJECT \
  --update-env-vars=PUBLIC_BASE_URL=$CHAT_URL \
  --update-secrets=STRIPE_SECRET_KEY=stripe-secret-key:latest   # "Approve and send" creates payment pages
gcloud run services update serviceiq-fsp --region=REGION --project=PROJECT \
  --update-env-vars=SERVICEIQ_WEBHOOK_URL=$API_URL/v1/events/fieldservice \
  --update-secrets=SERVICEIQ_WEBHOOK_SECRET=fsp-webhook-secret:latest
gcloud run jobs update serviceiq-sweep --region=REGION --project=PROJECT \
  --update-env-vars=PUBLIC_BASE_URL=$CHAT_URL --update-secrets=STRIPE_SECRET_KEY=stripe-secret-key:latest
```

**Reaching the API.** Stripe and the platform's `job.completed` events call `serviceiq-api` without a Google ID
token, but the API is private today. Every API route already checks a widget key (chat, reviews) or a signature
(Stripe, completion events), so the simplest option is to allow unauthenticated calls on `serviceiq-api`
(`gcloud run services add-iam-policy-binding serviceiq-api --member=allUsers --role=roles/run.invoker`). If it
must stay private, the platform's event can't arrive; the hourly sweep still bills completed jobs within an hour,
but Stripe payments then need a separate public webhook service. Decide before go-live.

In the Stripe dashboard, add the endpoint `$API_URL/v1/payments/stripe` for the events
`checkout.session.completed`, `checkout.session.async_payment_succeeded` and
`checkout.session.async_payment_failed`, and enable the payment methods card, US bank account (ACH) and Cash App
Pay. Locally: `stripe listen --forward-to localhost:8000/v1/payments/stripe`.

The sweep job now runs every hourly pass (`python -m serviceiq sweep`): approval timeouts, completed jobs whose
event was lost, payment reminders and review reminders.

## Rollback

Redeploy the previous image tag to `serviceiq-api`, `serviceiq-admin` and `serviceiq-chat`
(`gcloud run services update <service> --image=…:<previous tag>`), or set `AGENT_BOOKING_ENABLED=false` on the
API to keep intake but stop the agent from booking. The field-service platform and jobs can stay in place.
Phase 4: `AGENT_PAYMENTS_ENABLED=false` on the API, admin and sweep stops invoicing (completed jobs open a ticket
for the office to bill by hand); payments already made are unaffected.

## Deployed (October 29, 2026)

Project `serviceiq-512906`, region `us-central1`, image `serviceiq:883c284`: `serviceiq-fsp` (private),
jobs `serviceiq-fsp-seed` and `serviceiq-sweep`, Cloud Scheduler `serviceiq-sweep-hourly` (`0 * * * *` UTC);
`serviceiq-api`, `serviceiq-admin` and `serviceiq-chat` updated from `fd0d4e3`. Live check: a request booked
through the deployed API (test job cancelled afterwards).
