#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { resolveSettings } from '../lib/config';
import { KbAgentStack } from '../lib/kb-agent-stack';

const app = new App();
const settings = resolveSettings((key) => app.node.tryGetContext(key));

new KbAgentStack(app, `${settings.prefix}-${settings.env.envName}`, {
  settings,
  description:
    'AWS-native Knowledge Base Agent (RAG): authenticated API, in-memory vector retrieval, ' +
    'grounded answers with verified citations.',

  // No `env` on purpose. The stack resolves account and region from whichever credentials
  // the CLI is using, so one commit deploys to a personal development account and to the
  // AMCRO sandbox without edits. `cdk synth` therefore also works with no credentials at
  // all, which keeps the build reviewable offline.
});

app.synth();
