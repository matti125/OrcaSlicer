#include "SourceFileWatcher.hpp"

#include <boost/filesystem.hpp>
#include <boost/system/error_code.hpp>

namespace fs = boost::filesystem;

namespace Slic3r { namespace GUI {

namespace {
    // Sentinel for "file does not currently exist" so a create is detected as a change too.
    constexpr std::time_t source_file_missing_mtime = 0;

    std::time_t get_source_file_mtime(const std::string& path)
    {
        boost::system::error_code ec;
        std::time_t mtime = fs::last_write_time(path, ec);
        return ec ? source_file_missing_mtime : mtime;
    }
}

std::string SourceFileWatcher::resolve_source_file_path(const std::string& recorded_path,
                                                          const fs::path& project_folder)
{
    if (recorded_path.empty() || fs::exists(recorded_path))
        return recorded_path;
    if (!project_folder.empty()) {
        fs::path candidate = project_folder / fs::path(recorded_path).filename();
        if (fs::exists(candidate))
            return candidate.string();
    }
    return recorded_path;
}

SourceFileWatcher::SourceFileWatcher()
{
    m_debounce_timer.SetOwner(this, 0);
    Bind(wxEVT_TIMER, &SourceFileWatcher::on_timer, this);
    Bind(wxEVT_FSWATCHER, &SourceFileWatcher::on_fs_event, this);
}

SourceFileWatcher::~SourceFileWatcher()
{
    if (m_watcher != nullptr) {
        m_watcher->RemoveAll();
        delete m_watcher;
    }
}

void SourceFileWatcher::set_watched_files(std::set<std::string> resolved_paths)
{
    if (resolved_paths == m_watched_files)
        return;

    if (m_watcher == nullptr) {
        m_watcher = new wxFileSystemWatcher();
        m_watcher->SetOwner(this);
    }

    m_watcher->RemoveAll();
    m_watched_files = std::move(resolved_paths);

    // Record a baseline mtime for each tracked file so the debounced check can tell whether it
    // actually changed, rather than relying on the watcher event to name the file: directory-level
    // backends (e.g. macOS's kqueue-based one) can report a rename-into-place with just the
    // containing directory and no filename at all, which a path-matching approach would miss.
    std::map<std::string, std::time_t> new_mtimes;
    std::set<std::string> watched_dirs;
    for (const std::string& file : m_watched_files) {
        new_mtimes[file] = get_source_file_mtime(file);
        watched_dirs.insert(fs::path(file).parent_path().string());
    }
    m_mtimes = std::move(new_mtimes);

    for (const std::string& dir : watched_dirs) {
        if (!dir.empty() && fs::is_directory(dir))
            m_watcher->Add(wxFileName(dir, wxEmptyString));
    }

#ifndef _WIN32
    // The directory watch above only fires when the listing changes; an in-place overwrite of an
    // existing file needs a watch on the file itself. Not on Windows: wx's backend rejects
    // file-level watches with a wxLogError dialog, and ReadDirectoryChangesW already reports
    // in-place writes through the directory watch.
    for (const std::string& file : m_watched_files) {
        if (fs::exists(file))
            m_watcher->Add(wxFileName(file));
    }
#endif
}

void SourceFileWatcher::clear()
{
    if (m_watcher != nullptr)
        m_watcher->RemoveAll();
    m_watched_files.clear();
    m_mtimes.clear();
}

void SourceFileWatcher::forget_watched_files()
{
    m_watched_files.clear();
}

void SourceFileWatcher::on_fs_event(wxFileSystemWatcherEvent&)
{
    // Any event in a watched directory just wakes the debounced check below; see the comment in
    // set_watched_files() for why we don't try to match the event's reported path.
    m_debounce_timer.Start(500, wxTIMER_ONE_SHOT);
}

void SourceFileWatcher::on_timer(wxTimerEvent&)
{
    m_debounce_timer.Stop();
    if (files_changed_on_disk() && m_on_changed)
        m_on_changed();
}

bool SourceFileWatcher::files_changed_on_disk()
{
    bool changed = false;
    for (auto& [file, mtime] : m_mtimes) {
        std::time_t current = get_source_file_mtime(file);
        if (current != mtime) {
            mtime = current;
            changed = true;
        }
    }
    return changed;
}

}} // namespace Slic3r::GUI
