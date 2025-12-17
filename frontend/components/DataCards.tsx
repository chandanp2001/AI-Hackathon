import React from 'react';
import { format, parseISO } from 'date-fns';
import { 
  Calendar, 
  Clock, 
  MapPin, 
  Users, 
  Video, 
  ExternalLink,
  Mail,
  Paperclip,
  Eye,
  FileText,
  File,
  Table,
  Presentation,
  Folder,
  Share2,
  User
} from 'lucide-react';
import { CalendarEvent, EmailMessage, DriveFile } from '@/services/types';

// ============================================================================
// Calendar Event Card
// ============================================================================

interface CalendarEventCardProps {
  event: CalendarEvent;
}

export function CalendarEventCard({ event }: CalendarEventCardProps) {
  const startDate = parseISO(event.start_time);
  const endDate = parseISO(event.end_time);
  
  const formatTime = (date: Date) => format(date, 'h:mm a');
  const formatDate = (date: Date) => format(date, 'MMM d, yyyy');

  return (
    <div className="glass-card-hover p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="p-2 bg-blue-500/20 rounded-lg">
            <Calendar className="w-4 h-4 text-blue-400" />
          </div>
          <div>
            <h4 className="font-medium text-white">{event.title}</h4>
            <p className="text-xs text-slate-400">
              {event.status === 'confirmed' ? 'Confirmed' : event.status}
            </p>
          </div>
        </div>
        {event.meeting_link && (
          <a
            href={event.meeting_link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 px-2 py-1 bg-green-500/20 text-green-400 
                       text-xs rounded-lg hover:bg-green-500/30 transition-all"
          >
            <Video className="w-3 h-3" />
            Join
          </a>
        )}
      </div>

      <div className="space-y-2 text-sm">
        <div className="flex items-center gap-2 text-slate-300">
          <Clock className="w-4 h-4 text-slate-500" />
          <span>
            {event.is_all_day ? (
              formatDate(startDate)
            ) : (
              <>
                {formatDate(startDate)} • {formatTime(startDate)} - {formatTime(endDate)}
              </>
            )}
          </span>
        </div>

        {event.location && (
          <div className="flex items-center gap-2 text-slate-300">
            <MapPin className="w-4 h-4 text-slate-500" />
            <span className="truncate">{event.location}</span>
          </div>
        )}

        {event.attendees.length > 0 && (
          <div className="flex items-center gap-2 text-slate-300">
            <Users className="w-4 h-4 text-slate-500" />
            <span className="truncate">
              {event.attendees.slice(0, 3).join(', ')}
              {event.attendees.length > 3 && ` +${event.attendees.length - 3} more`}
            </span>
          </div>
        )}
      </div>

      {event.description && (
        <p className="text-xs text-slate-400 line-clamp-2 border-t border-slate-700/50 pt-2">
          {event.description}
        </p>
      )}
    </div>
  );
}

// ============================================================================
// Email Message Card
// ============================================================================

interface EmailCardProps {
  email: EmailMessage;
}

export function EmailCard({ email }: EmailCardProps) {
  const date = parseISO(email.date);

  return (
    <div className="glass-card-hover p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="p-2 bg-red-500/20 rounded-lg">
            <Mail className="w-4 h-4 text-red-400" />
          </div>
          <div className="min-w-0">
            <h4 className="font-medium text-white truncate">{email.subject}</h4>
            <p className="text-xs text-slate-400 truncate">From: {email.sender}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {email.is_unread && (
            <span className="w-2 h-2 bg-blue-400 rounded-full" title="Unread" />
          )}
          {email.has_attachments && (
            <Paperclip className="w-4 h-4 text-slate-500" title="Has attachments" />
          )}
        </div>
      </div>

      <div className="text-sm text-slate-300">
        <p className="line-clamp-2">{email.snippet || email.body_preview}</p>
      </div>

      <div className="flex items-center justify-between text-xs text-slate-500 
                      border-t border-slate-700/50 pt-2">
        <span>{format(date, 'MMM d, yyyy • h:mm a')}</span>
        <div className="flex items-center gap-2">
          {email.labels.slice(0, 2).map((label) => (
            <span 
              key={label} 
              className="px-2 py-0.5 bg-slate-700/50 rounded text-slate-400"
            >
              {label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Drive File Card
// ============================================================================

interface DriveFileCardProps {
  file: DriveFile;
}

const mimeTypeIcons: Record<string, React.ComponentType<{ className?: string }>> = {
  'application/vnd.google-apps.document': FileText,
  'application/vnd.google-apps.spreadsheet': Table,
  'application/vnd.google-apps.presentation': Presentation,
  'application/vnd.google-apps.folder': Folder,
  'application/pdf': File,
};

function getFileIcon(mimeType: string) {
  return mimeTypeIcons[mimeType] || File;
}

function getFileTypeLabel(mimeType: string): string {
  const labels: Record<string, string> = {
    'application/vnd.google-apps.document': 'Google Doc',
    'application/vnd.google-apps.spreadsheet': 'Google Sheet',
    'application/vnd.google-apps.presentation': 'Google Slides',
    'application/vnd.google-apps.folder': 'Folder',
    'application/pdf': 'PDF',
  };
  return labels[mimeType] || 'File';
}

export function DriveFileCard({ file }: DriveFileCardProps) {
  const FileIcon = getFileIcon(file.mime_type);
  const modifiedDate = file.modified_time ? parseISO(file.modified_time) : null;

  return (
    <div className="glass-card-hover p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="p-2 bg-yellow-500/20 rounded-lg">
            <FileIcon className="w-4 h-4 text-yellow-400" />
          </div>
          <div className="min-w-0">
            <h4 className="font-medium text-white truncate">{file.name}</h4>
            <p className="text-xs text-slate-400">{getFileTypeLabel(file.mime_type)}</p>
          </div>
        </div>
        {file.web_view_link && (
          <a
            href={file.web_view_link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 px-2 py-1 bg-slate-700 text-slate-300 
                       text-xs rounded-lg hover:bg-slate-600 transition-all"
          >
            <ExternalLink className="w-3 h-3" />
            Open
          </a>
        )}
      </div>

      <div className="space-y-2 text-sm">
        {file.owners.length > 0 && (
          <div className="flex items-center gap-2 text-slate-300">
            <User className="w-4 h-4 text-slate-500" />
            <span className="truncate">{file.owners[0]}</span>
          </div>
        )}

        {modifiedDate && (
          <div className="flex items-center gap-2 text-slate-400 text-xs">
            <Clock className="w-4 h-4 text-slate-500" />
            <span>Modified {format(modifiedDate, 'MMM d, yyyy')}</span>
          </div>
        )}
        
        {file.shared && (
          <div className="flex items-center gap-2 text-slate-400 text-xs">
            <Share2 className="w-4 h-4 text-slate-500" />
            <span>Shared</span>
          </div>
        )}
      </div>

      {file.content_preview && (
        <p className="text-xs text-slate-400 line-clamp-2 border-t border-slate-700/50 pt-2">
          {file.content_preview}
        </p>
      )}
    </div>
  );
}

// ============================================================================
// Data Cards Grid
// ============================================================================

interface DataCardsProps {
  rawData?: Record<string, unknown>;
}

export function DataCards({ rawData }: DataCardsProps) {
  if (!rawData) return null;

  const calendarEvents = rawData.calendar as CalendarEvent[] | undefined;
  const emails = rawData.gmail as EmailMessage[] | undefined;
  const files = rawData.drive as DriveFile[] | undefined;

  const hasData = calendarEvents?.length || emails?.length || files?.length;
  if (!hasData) return null;

  return (
    <div className="space-y-4 mt-4">
      {calendarEvents && calendarEvents.length > 0 && (
        <div>
          <h5 className="text-xs uppercase tracking-wider text-slate-500 mb-2 font-medium">
            Calendar Events
          </h5>
          <div className="grid gap-3">
            {calendarEvents.slice(0, 5).map((event) => (
              <CalendarEventCard key={event.event_id} event={event} />
            ))}
          </div>
        </div>
      )}

      {emails && emails.length > 0 && (
        <div>
          <h5 className="text-xs uppercase tracking-wider text-slate-500 mb-2 font-medium">
            Emails
          </h5>
          <div className="grid gap-3">
            {emails.slice(0, 5).map((email) => (
              <EmailCard key={email.message_id} email={email} />
            ))}
          </div>
        </div>
      )}

      {files && files.length > 0 && (
        <div>
          <h5 className="text-xs uppercase tracking-wider text-slate-500 mb-2 font-medium">
            Files
          </h5>
          <div className="grid gap-3">
            {files.slice(0, 5).map((file) => (
              <DriveFileCard key={file.file_id} file={file} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

