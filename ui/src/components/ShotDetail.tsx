import type { Shot } from '../types/shot';
import { getServerOrigin } from '../utils/serverOrigin';
import { ShotDisplay } from './ShotDisplay';
import './ShotDetail.css';

interface ShotDetailProps {
  shot: Shot;
  onClose: () => void;
}

export function ShotDetail({ shot, onClose }: ShotDetailProps) {
  const videoUrl =
    shot.session_id && shot.shot_number !== undefined
      ? `${getServerOrigin()}/api/shots/${shot.session_id}/${shot.shot_number}/video`
      : null;

  return (
    <div className="shot-detail-overlay" onClick={onClose}>
      <div className="shot-detail" onClick={(e) => e.stopPropagation()}>
        <button className="shot-detail__close" onClick={onClose} aria-label="Close">
          ×
        </button>

        <div className="shot-detail__video">
          {videoUrl ? (
            <video controls src={videoUrl} className="shot-detail__video-player" />
          ) : (
            <div className="shot-detail__video-placeholder">
              {shot.shot_number === undefined
                ? 'No video recorded for this shot'
                : 'Recording video…'}
            </div>
          )}
        </div>

        <div className="shot-detail__metrics">
          <ShotDisplay shot={shot} />
        </div>
      </div>
    </div>
  );
}
