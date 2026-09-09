import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { StackSettings } from './config';
/**
 * Placeholder written into the provider API key secret at creation time.
 *
 * Shared contract with services/ingest/handler.py, which treats it as "no key configured"
 * and skips seeding cleanly instead of failing the deployment. Not a secret: it is a marker,
 * so having it visible in the template is harmless and intentional.
 */
export declare const PROVIDER_KEY_PLACEHOLDER = "REPLACE_WITH_PROVIDER_API_KEY";
export interface KbAgentStackProps extends StackProps {
    readonly settings: StackSettings;
}
/**
 * The knowledge base agent, as one stack composed of constructs (ADR-05).
 *
 * One stack rather than several: cross-stack references become CloudFormation exports, and
 * CloudFormation refuses to delete a stack whose exports are in use. For a reviewer who
 * deploys and destroys once, that is friction with no benefit. Constructs give the same
 * modularity without it.
 */
export declare class KbAgentStack extends Stack {
    constructor(scope: Construct, id: string, props: KbAgentStackProps);
    /**
     * Optional account guard.
     *
     * Deliberately opt-in. A hard-coded account would defeat the point of an
     * environment-agnostic stack: the same commit has to deploy to a personal development
     * account and to the AMCRO sandbox. Passing `-c expectedAccount=<id>` turns on the check
     * for people who keep several profiles and would rather fail than deploy to the wrong one.
     */
    private guardAccount;
}
