# Deploying the Phase 2–3 additions to Google Cloud Run

This adds three things to the existing ServiceIQ deployment (API, chat and admin on Cloud Run, Cloud SQL
Postgres, Secret Manager):

| Piece | Cloud Run | Image role | Reaches |
|---|---|---|---|
| Field-service platform | service `serviceiq-fsp`, **private** | `fsp` | Cloud SQL database `fieldservice` |
| Contractor onboarding | job `serviceiq-fsp-seed` | `fsp-seed` | same |
| Approval timeouts | job `serviceiq-sweep`, run hourly by Cloud Scheduler | `sweep` | ServiceIQ database, `serviceiq-fsp` |

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
# Postgres 15+: let fsp_app create its tables in the public schema
gcloud sql connect INSTANCE --user=postgres --database=fieldservice --project=PROJECT
#   fieldservice=> GRANT ALL ON SCHEMA public TO fsp_app;
```

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
