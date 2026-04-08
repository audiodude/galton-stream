# CLAUDE.md

Read README.md first for project architecture and deployment details.

If you ever find yourself telling the user "Now try <action>", or "Don't forget to <action>" or anything similar, instead offer to do it yourself. So say this: "Would you like me to <action> now for you?"

## Deployment

Railway deploys from the `release` branch, NOT `main`. After pushing changes to `main`, merge to `release` and push to deploy. Use regular `git merge main` (NOT squash merge, which causes persistent conflicts). galton-stream and galton-monitor are separate Railway services in the same repo with watch paths so they deploy independently. Watch patterns must include both `/*` (root files) and `/**` (subdirectory files).

## Railway

The Railway CLI (`railway`) is mostly broken and unreliable for mutations (creating services, setting variables, etc.). Use the Railway GraphQL API directly at `https://backboard.railway.com/graphql/v2` instead. Read-only commands like `railway service logs` and `railway status` generally work. For anything that creates or modifies resources, use the API or direct the user to the Railway dashboard.
