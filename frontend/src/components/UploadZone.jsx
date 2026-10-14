import { useState, useRef, useCallback } from "react";
import { Upload, FileArchive, FileCode, FolderOpen, X } from "lucide-react";

const MAX_ZIP_SIZE_MB = 500;
const MAX_ZIP_SIZE_BYTES = MAX_ZIP_SIZE_MB * 1024 * 1024;
const MAX_HTML_SIZE_MB = 50;
const MAX_HTML_SIZE_BYTES = MAX_HTML_SIZE_MB * 1024 * 1024;

const UPLOAD_MODES = [
  { key: "zip", label: "ZIP Archive", icon: FileArchive, accept: ".zip,application/zip" },
  { key: "html", label: "HTML File", icon: FileCode, accept: ".html,.htm" },
  { key: "folder", label: "Folder", icon: FolderOpen, accept: "" },
];

/**
 * Multi-mode upload zone: ZIP archive, single HTML file, or folder.
 *
 * @param {{ onFilesSelected: (data: { type: string, files: File[] } | null) => void }} props
 */
export default function UploadZone({ onFilesSelected }) {
  const [uploadType, setUploadType] = useState("zip");
  const [isDragging, setIsDragging] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState(null);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  const validateFiles = useCallback(
    (fileList, type) => {
      if (!fileList || fileList.length === 0) return "No file selected.";

      if (type === "zip") {
        const file = fileList[0];
        if (!file.name.toLowerCase().endsWith(".zip") && file.type !== "application/zip") {
          return "Only .zip files are accepted.";
        }
        if (file.size > MAX_ZIP_SIZE_BYTES) {
          return `File exceeds the ${MAX_ZIP_SIZE_MB}MB limit.`;
        }
      } else if (type === "html") {
        const file = fileList[0];
        const name = file.name.toLowerCase();
        if (!name.endsWith(".html") && !name.endsWith(".htm")) {
          return "Only .html or .htm files are accepted.";
        }
        if (file.size > MAX_HTML_SIZE_BYTES) {
          return `File exceeds the ${MAX_HTML_SIZE_MB}MB limit.`;
        }
      } else if (type === "folder") {
        let totalSize = 0;
        let hasHtml = false;
        for (const f of fileList) {
          totalSize += f.size;
          const name = f.name.toLowerCase();
          if (name.endsWith(".html") || name.endsWith(".htm")) {
            hasHtml = true;
          }
        }
        if (!hasHtml) {
          return "Folder must contain at least one .html file.";
        }
        if (totalSize > MAX_ZIP_SIZE_BYTES) {
          return `Total folder size exceeds the ${MAX_ZIP_SIZE_MB}MB limit.`;
        }
      }
      return null;
    },
    []
  );

  const handleFiles = useCallback(
    (fileList, type) => {
      const arr = Array.from(fileList);
      const validationError = validateFiles(arr, type);
      if (validationError) {
        setError(validationError);
        setSelectedFiles(null);
        onFilesSelected(null);
        return;
      }
      setError(null);
      setSelectedFiles(arr);
      onFilesSelected({ type, files: arr });
    },
    [validateFiles, onFilesSelected]
  );

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (e) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);

      if (uploadType === "folder") {
        setError("Use the browse button to select a folder.");
        return;
      }
      const file = e.dataTransfer.files?.[0];
      if (file) handleFiles([file], uploadType);
    },
    [handleFiles, uploadType]
  );

  const handleInputChange = useCallback(
    (e) => {
      const fileList = e.target.files;
      if (fileList && fileList.length > 0) {
        handleFiles(fileList, uploadType);
      }
      e.target.value = "";
    },
    [handleFiles, uploadType]
  );

  const handleRemove = useCallback(() => {
    setSelectedFiles(null);
    setError(null);
    onFilesSelected(null);
  }, [onFilesSelected]);

  const switchMode = useCallback(
    (newType) => {
      if (newType === uploadType) return;
      setUploadType(newType);
      setSelectedFiles(null);
      setError(null);
      onFilesSelected(null);
    },
    [uploadType, onFilesSelected]
  );

  const formatSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const currentMode = UPLOAD_MODES.find((m) => m.key === uploadType);

  const hintText = {
    zip: `.zip files only, max ${MAX_ZIP_SIZE_MB}MB`,
    html: `.html / .htm files only, max ${MAX_HTML_SIZE_MB}MB`,
    folder: `Select a folder containing HTML, CSS, JS, images (max ${MAX_ZIP_SIZE_MB}MB total)`,
  };

  const dropText = {
    zip: "zip file",
    html: "HTML file",
    folder: "folder",
  };

  // Summary for selected files display
  const selectionSummary = () => {
    if (!selectedFiles) return null;
    if (uploadType === "folder") {
      const total = selectedFiles.reduce((s, f) => s + f.size, 0);
      return { name: `${selectedFiles.length} files`, size: total };
    }
    return { name: selectedFiles[0].name, size: selectedFiles[0].size };
  };

  const summary = selectionSummary();
  const SelectedIcon = currentMode?.icon || FileArchive;

  return (
    <div className="w-full">
      {/* ── Tab selector ─────────────────────────────────────────── */}
      <div className="flex gap-1 p-1 mb-4 bg-gray-100 rounded-lg">
        {UPLOAD_MODES.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => switchMode(key)}
            className={`flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-md text-sm font-medium transition-all
              ${
                uploadType === key
                  ? "bg-white text-[#2563EB] shadow-sm"
                  : "text-gray-500 hover:text-gray-700"
              }`}
          >
            <Icon className="w-4 h-4" />
            {label}
          </button>
        ))}
      </div>

      {/* ── Drop zone / file preview ─────────────────────────────── */}
      {!summary ? (
        <div
          role="button"
          tabIndex={0}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
          }}
          className={`relative flex flex-col items-center justify-center gap-4 p-12
            border-2 border-dashed rounded-xl cursor-pointer transition-all duration-200
            ${
              isDragging
                ? "border-[#2563EB] bg-blue-50 scale-[1.01]"
                : "border-gray-300 bg-white hover:border-[#2563EB] hover:bg-gray-50"
            }
            ${error ? "border-red-400 bg-red-50" : ""}`}
        >
          <Upload
            className={`w-12 h-12 ${
              isDragging ? "text-[#2563EB]" : "text-gray-400"
            } transition-colors`}
          />
          <div className="text-center">
            <p className="text-lg font-semibold text-gray-700">
              {isDragging
                ? `Drop your ${dropText[uploadType]} here`
                : uploadType === "folder"
                ? "Click to select a folder"
                : `Drag & drop your ${dropText[uploadType]} here`}
            </p>
            <p className="mt-1 text-sm text-gray-500">
              or <span className="text-[#2563EB] font-medium">click to browse</span>
            </p>
            <p className="mt-2 text-xs text-gray-400">{hintText[uploadType]}</p>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={currentMode?.accept || ""}
            {...(uploadType === "folder" ? { webkitdirectory: "", directory: "" } : {})}
            onChange={handleInputChange}
            className="hidden"
            aria-label={`Upload ${dropText[uploadType]}`}
          />
        </div>
      ) : (
        <div className="flex items-center gap-4 p-6 bg-white border border-gray-200 rounded-xl">
          <div className="flex items-center justify-center w-12 h-12 bg-blue-50 rounded-lg">
            <SelectedIcon className="w-6 h-6 text-[#2563EB]" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-gray-900 truncate">
              {summary.name}
            </p>
            <p className="text-sm text-gray-500">{formatSize(summary.size)}</p>
          </div>
          <button
            type="button"
            onClick={handleRemove}
            className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-md transition-colors"
            aria-label="Remove file"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
      )}

      {error && (
        <p className="mt-2 text-sm text-red-600 font-medium">{error}</p>
      )}
    </div>
  );
}
