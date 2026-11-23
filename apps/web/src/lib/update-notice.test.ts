import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  detectHostKind,
  isReleaseVersion,
  shouldShowUpdateNotice,
  unknownUpdateStatus,
  updateCommand,
  type UpdateNoticeResponse,
} from './update-notice';

function response(overrides: Partial<UpdateNoticeResponse> = {}): UpdateNoticeResponse {
  return {
    status: 'update_available',
    current: '1.0.20',
    latest: '1.0.21',
    release_url: 'https://github.com/sergiovision/DevServer/releases/tag/v1.0.21',
    checked_at: '2026-11-23T00:00:00Z',
    dismissed_build: null,
    ...overrides,
  };
}

describe('shouldShowUpdateNotice', () => {
  // Feature: F-UPD-02
  it('shows an available update that was never dismissed', () => {
    assert.equal(shouldShowUpdateNotice(response()), true);
  });

  // Feature: F-UPD-02
  it('hides the release that was dismissed', () => {
    assert.equal(shouldShowUpdateNotice(response({ dismissed_build: '1.0.21' })), false);
  });

  // Feature: F-UPD-02
  it('shows a later release even after an earlier one was dismissed', () => {
    assert.equal(
      shouldShowUpdateNotice(response({ latest: '1.0.22', dismissed_build: '1.0.21' })),
      true,
    );
  });

  // Feature: F-UPD-03
  it('shows nothing when the check failed or the status is unknown', () => {
    assert.equal(shouldShowUpdateNotice(null), false);
    assert.equal(shouldShowUpdateNotice(undefined), false);
    assert.equal(shouldShowUpdateNotice({ ...unknownUpdateStatus('1.0.20'), dismissed_build: null }), false);
  });

  // Feature: F-UPD-03
  it('shows nothing when up to date, disabled, or the latest version is malformed', () => {
    assert.equal(shouldShowUpdateNotice(response({ status: 'up_to_date' })), false);
    assert.equal(shouldShowUpdateNotice(response({ status: 'disabled' })), false);
    assert.equal(shouldShowUpdateNotice(response({ latest: null })), false);
    assert.equal(shouldShowUpdateNotice(response({ latest: 'v1.0.21<script>' })), false);
  });
});

describe('isReleaseVersion', () => {
  // Feature: F-UPD-02
  it('accepts only a bare major.minor.build', () => {
    assert.equal(isReleaseVersion('1.0.21'), true);
    assert.equal(isReleaseVersion('v1.0.21'), false);
    assert.equal(isReleaseVersion('1.0'), false);
    assert.equal(isReleaseVersion(21), false);
  });
});

describe('update command per host', () => {
  // Feature: F-UPD-06
  it('uses the docker start script for a docker build regardless of OS', () => {
    const host = detectHostKind({ buildVersion: '1.0.20.docker', platform: 'Win32' });
    assert.equal(host, 'docker');
    assert.equal(updateCommand(host), 'git pull && ./scripts/start.sh --docker');
  });

  // Feature: F-UPD-06
  it('uses the PowerShell restart script on Windows', () => {
    const host = detectHostKind({
      buildVersion: '1.0.20.production',
      platform: 'Win32',
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    });
    assert.equal(host, 'windows');
    assert.equal(updateCommand(host), 'git pull; ./scripts/restart.ps1');
  });

  // Feature: F-UPD-06
  it('uses the shell restart script on macOS and Linux', () => {
    for (const platform of ['MacIntel', 'Linux x86_64', 'darwin']) {
      const host = detectHostKind({ buildVersion: '1.0.20.production', platform });
      assert.equal(host, 'unix');
      assert.equal(updateCommand(host), 'git pull && ./scripts/restart.sh --prod');
    }
  });
});
