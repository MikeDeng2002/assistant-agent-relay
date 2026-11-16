# Hosted study

The hosted study uses Supabase for email/password login, study results, and recordings.
Participants go to `/study`. Researchers go to `/research`.
The hosted site supports copying prompts to an agent's chat app and pasting replies.
The Mac bench console still handles automatic iMessage in its local study flow.

## Database and accounts

1. In the Supabase project, enable the Data API. Apply the [study migrations](../supabase/migrations/) in order,
   either in SQL Editor or through an authenticated Supabase CLI migration.
2. In **Authentication → Users**, create an account for each invited participant and
   researcher. Give each a unique, privately delivered password. For an internal
   test with a dummy address, **Create new user** with **Auto confirm user** avoids
   sending mail. The site has no public registration form.
3. In SQL Editor, bind each account to its study role. Replace the example emails
   and participant ID with the actual values.

   ```sql
   insert into public.study_members (user_id, pid, role)
   select id, 'P001', 'participant' from auth.users
   where email = 'participant@example.com';

   insert into public.study_members (user_id, pid, role)
   select id, null, 'researcher' from auth.users
   where email = 'researcher@example.com';
   ```

The invite list is enforced in the database. Set a member’s `enabled` field to
`false` to withdraw access and retain their results. Participants can read and write their
own result files. Researchers can read all study results and recordings and
download results with the change history. The web server uses the publishable
key and each person's login session.

## Start the website

Set these environment variables locally or on the hosting platform:

| Variable | Value |
| --- | --- |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_PUBLISHABLE_KEY` | Key starting with `sb_publishable_` |
| `STUDY_TASK` | Task name from its `run.json`, defaults to `Restaurant` |
| `STUDY_TASKS_DIR` | Path to the private task directory containing the task's `run.json` and `study.json` |
| `STUDY_AGENTS` | Comma-separated names and channels, such as `instinct:imessage` |
| `STUDY_ORIGIN` | The exact site origin, such as `https://study.example.com`, for remote hosting |

The task directory stays private on the host. It is not part of the public
repository. For a local run with the variables in an ignored `.env` file:

```sh
uv run --env-file .env python -m bench.hosted_study --port 8874
```

The local site opens at `http://127.0.0.1:8874/study`. Behind an HTTPS
hosting proxy, start the same service with `--bind 0.0.0.0` and let the host
set `PORT`. Share the hosted `/study` URL with participants and the hosted
`/research` URL with the team.

For a smoke test, complete a fictional study day, click **Submit results**, and
reload the page. Sign in separately as a researcher and confirm the participant
shows **Submitted day 1** and that the saved answers and conversation are readable.
