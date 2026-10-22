# dev-utils
Collection of scripts I find useful in my day to day work

## netlify_env.py

Sets environment variables on several Netlify projects in one go. It logs in through a Netlify OAuth app and asks for the client id and secret each time it runs, so no credentials are stored. Python 3 standard library only.

```sh
./netlify_env.py --sites SITE_ID_1,SITE_ID_2 --env ENV1=value1 --env "ENV2=a,b"
```

Repeat `--env` for each variable. Values can contain commas and `=`. Add `--rebuild` to trigger a production build on each project where at least one variable was set, so the new values take effect.

One-time setup: create an OAuth application in Netlify (User settings → Applications → OAuth → New OAuth app) and set its redirect URI to `http://localhost:8888/callback`. If you use a different port, set the URI to match it and pass `--port`.

If a variable already exists, its value is replaced for all deploy contexts. Its scopes and secret flag are kept.
