'use client';

import React, { useState } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CForm,
  CFormLabel,
  CFormInput,
  CFormSwitch,
  CFormSelect,
  CButton,
  CAlert,
  CRow,
  CCol,
} from '@coreui/react-pro';
import { QueueStats } from './QueueStats';

interface SettingsFormProps {
  settings: Record<string, unknown>;
}

export function SettingsForm({ settings: initial }: SettingsFormProps) {
  const [settings, setSettings] = useState<Record<string, unknown>>(initial);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const updateSetting = async (key: string, value: unknown) => {
    setSaving(true);
    setSaved(false);
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, value }),
      });
      if (res.ok) {
        setSettings((prev) => ({ ...prev, [key]: value }));
        setSaved(true);
        setTimeout(() => setSaved(false), 2000);
      }
    } catch {
      // ignore
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      {/* Queue Stats */}
      <CCard className="mb-4">
        <CCardHeader><strong>Queue Status</strong></CCardHeader>
        <CCardBody>
          <QueueStats />
        </CCardBody>
      </CCard>

      {/* General Settings */}
      <CCard className="mb-4">
        <CCardHeader><strong>General</strong></CCardHeader>
        <CCardBody>
          {saved && <CAlert color="success">Settings saved.</CAlert>}
          <CForm>
            <CRow className="mb-3">
              <CCol md={4}>
                <CFormLabel>Execution Mode</CFormLabel>
                <CFormSelect
                  value={String(settings.execution_mode || 'autonomous')}
                  onChange={(e) => updateSetting('execution_mode', e.target.value)}
                >
                  <option value="autonomous">Autonomous</option>
                  <option value="interactive">Interactive</option>
                  <option value="paused">Paused</option>
                </CFormSelect>
              </CCol>
              <CCol md={4}>
                <CFormLabel>Max Concurrency</CFormLabel>
                <CFormInput
                  type="number"
                  min="1"
                  max="10"
                  value={String(settings.max_concurrency || 2)}
                  onChange={(e) => updateSetting('max_concurrency', parseInt(e.target.value))}
                />
              </CCol>
            </CRow>

            <CRow className="mb-3">
              <CCol md={4}>
                <CFormSwitch
                  label="Queue Paused"
                  checked={Boolean(settings.queue_paused)}
                  onChange={(e) => updateSetting('queue_paused', e.target.checked)}
                />
              </CCol>
              <CCol md={4}>
                <CFormSwitch
                  label="Auto-enqueue on create"
                  checked={Boolean(settings.auto_enqueue)}
                  onChange={(e) => updateSetting('auto_enqueue', e.target.checked)}
                />
              </CCol>
              <CCol md={4}>
                <CFormSwitch
                  label="Notifications enabled"
                  checked={Boolean(settings.notifications_enabled)}
                  onChange={(e) => updateSetting('notifications_enabled', e.target.checked)}
                />
              </CCol>
            </CRow>
          </CForm>
        </CCardBody>
      </CCard>
    </>
  );
}
