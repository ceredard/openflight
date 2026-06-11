import { useMemo, useState } from 'react';
import type { Shot } from '../types/shot';
import { useUnitPreference } from '../state/useUnitPreference';
import { formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit } from '../utils/units';
import { getServerOrigin } from '../utils/serverOrigin';
import { Pagination } from './Pagination';
import './ShotReplay.css';

const SHOTS_PER_PAGE = 5;
const MAX_VIDEO_RETRIES = 6;

interface ShotReplayProps {
  shots: Shot[];
}

interface ShotVideoProps {
  videoUrl: string;
}

/**
 * Plays a shot's replay clip, retrying with backoff while the recording is
 * still being saved/muxed on the server.
 */
function ShotVideo({ videoUrl }: ShotVideoProps) {
  const [attempt, setAttempt] = useState(0);
  const [exhausted, setExhausted] = useState(false);

  const handleError = () => {
    if (attempt >= MAX_VIDEO_RETRIES) {
      setExhausted(true);
      return;
    }
    setTimeout(() => setAttempt((a) => a + 1), 1000 * (attempt + 1));
  };

  if (exhausted) {
    return (
      <div className="shot-replay__video-placeholder">
        Video is taking longer than expected to process.
      </div>
    );
  }

  return (
    <video
      key={attempt}
      className="shot-replay__video-player"
      controls
      playsInline
      src={`${getServerOrigin()}${videoUrl}?retry=${attempt}`}
      onError={handleError}
    />
  );
}

export function ShotReplay({ shots }: ShotReplayProps) {
  const [page, setPage] = useState(0);
  const [selectedTimestamp, setSelectedTimestamp] = useState<string | null>(null);
  const { unitSystem } = useUnitPreference();
  const speedUnit = getSpeedUnit(unitSystem);
  const distanceUnit = getDistanceUnit(unitSystem);

  const reversed = useMemo(() => [...shots].reverse(), [shots]);
  const totalPages = Math.ceil(reversed.length / SHOTS_PER_PAGE);
  const startIndex = page * SHOTS_PER_PAGE;
  const pageShots = reversed.slice(startIndex, startIndex + SHOTS_PER_PAGE);

  const selectedShot = useMemo(() => {
    if (selectedTimestamp) {
      const found = shots.find((s) => s.timestamp === selectedTimestamp);
      if (found) return found;
    }
    return reversed[0] ?? null;
  }, [shots, reversed, selectedTimestamp]);

  if (shots.length === 0) {
    return (
      <div className="shot-replay shot-replay--empty">
        <p>No shots recorded yet</p>
      </div>
    );
  }

  return (
    <div className="shot-replay">
      <div className="shot-replay__list">
        <div className="shot-replay__rows">
          {pageShots.map((shot, index) => {
            const shotNumber = shots.length - startIndex - index;
            const isSelected = shot.timestamp === selectedShot?.timestamp;
            return (
              <button
                key={shot.timestamp}
                className={`shot-replay__row ${isSelected ? 'shot-replay__row--active' : ''}`}
                onClick={() => setSelectedTimestamp(shot.timestamp)}
              >
                <span className="shot-replay__row-number">#{shotNumber}</span>
                <span className="shot-replay__row-club">{shot.club}</span>
                <span className="shot-replay__row-stat">
                  {formatSpeed(shot.ball_speed_mph, unitSystem, 1)} {speedUnit}
                </span>
                <span className="shot-replay__row-stat">
                  {formatDistance(shot.estimated_carry_yards, unitSystem, 0)} {distanceUnit}
                </span>
                {shot.video_url && (
                  <span className="shot-replay__row-video-dot" title="Video available" />
                )}
              </button>
            );
          })}
        </div>
        <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
      </div>

      {selectedShot && (
        <div className="shot-replay__detail">
          <div className="shot-replay__video">
            {selectedShot.video_url ? (
              <ShotVideo key={selectedShot.video_url} videoUrl={selectedShot.video_url} />
            ) : (
              <div className="shot-replay__video-placeholder">No video for this shot</div>
            )}
          </div>

          <div className="shot-replay__stats">
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">
                {formatSpeed(selectedShot.ball_speed_mph, unitSystem, 1)}
              </span>
              <span className="shot-replay__stat-label">Ball Speed ({speedUnit})</span>
            </div>
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">
                {selectedShot.club_speed_mph !== null
                  ? formatSpeed(selectedShot.club_speed_mph, unitSystem, 1)
                  : '—'}
              </span>
              <span className="shot-replay__stat-label">Club Speed ({speedUnit})</span>
            </div>
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">
                {formatDistance(selectedShot.estimated_carry_yards, unitSystem, 0)}
              </span>
              <span className="shot-replay__stat-label">Carry ({distanceUnit})</span>
            </div>
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">
                {selectedShot.launch_angle_vertical !== null
                  ? `${selectedShot.launch_angle_vertical.toFixed(1)}°`
                  : '—'}
              </span>
              <span className="shot-replay__stat-label">Launch Angle</span>
            </div>
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">
                {selectedShot.spin_rpm !== null
                  ? selectedShot.spin_rpm.toLocaleString('en-US', { maximumFractionDigits: 0 })
                  : '—'}
              </span>
              <span className="shot-replay__stat-label">Spin (rpm)</span>
            </div>
            <div className="shot-replay__stat">
              <span className="shot-replay__stat-value">{selectedShot.club}</span>
              <span className="shot-replay__stat-label">Club</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
