#ifndef slic3r_GUI_SourceFileWatcher_hpp_
#define slic3r_GUI_SourceFileWatcher_hpp_

#include <wx/event.h>
#include <wx/fswatcher.h>
#include <wx/timer.h>

#include <boost/filesystem/path.hpp>

#include <cstdint>
#include <ctime>
#include <functional>
#include <map>
#include <set>
#include <string>

namespace Slic3r { namespace GUI {

// Watches the on-disk source files referenced by the loaded model and notifies its owner once a
// tracked file's content has actually changed. Self-contained and reusable: it knows nothing
// about Model/ModelVolume, Plater, or what "reload" means -- the caller supplies the set of
// resolved paths to watch and a callback to run once a change is confirmed.
//
// Two watches cover each other's blind spot: most exporters write a temp file and rename it into
// place (only visible as a directory-listing change), while an in-place overwrite produces no
// directory event at all and needs a watch on the file itself. The event handler doesn't try to
// match the reported path -- macOS's kqueue backend can report a rename with just the directory
// and no filename -- it only wakes a debounced mtime comparison against the baseline recorded in
// set_watched_files(). Per-file watches are skipped on Windows: wx's MSW backend rejects them
// with an error dialog, and its ReadDirectoryChangesW directory watch already reports in-place
// writes.
class SourceFileWatcher : public wxEvtHandler
{
public:
    // Resolves a volume's recorded source path against a project folder fallback: a volume's
    // recorded source can be a bare filename rather than a full path (a 3MF saved without "Store
    // full source file paths in projects" only keeps the filename). Falls back to the recorded
    // path unchanged if it already exists or nothing is found next to the project.
    static std::string resolve_source_file_path(const std::string& recorded_path,
                                                 const boost::filesystem::path& project_folder);

    SourceFileWatcher();
    ~SourceFileWatcher() override;

    SourceFileWatcher(const SourceFileWatcher&) = delete;
    SourceFileWatcher& operator=(const SourceFileWatcher&) = delete;

    // Invoked (after the debounce delay) once a watched file's mtime is confirmed changed.
    void set_on_changed(std::function<void()> on_changed) { m_on_changed = std::move(on_changed); }

    // Replaces the set of watched files (already resolved to their on-disk paths) and rearms the
    // underlying watches. No-op if the set is unchanged from the last call.
    void set_watched_files(std::set<std::string> resolved_paths);

    // Drops all watches and the tracked baseline, e.g. when the feature is turned off.
    void clear();

    // Forgets which files are currently watched (without touching the on/off state) so the next
    // call to set_watched_files() re-arms from scratch, even if the resolved path set itself is
    // unchanged. A rename-into-place leaves a file-level watch bound to the old inode, so the
    // set of paths looking the same does not mean the watch is still live.
    void forget_watched_files();

private:
    void on_fs_event(wxFileSystemWatcherEvent& evt);
    void on_timer(wxTimerEvent& evt);
    // Compares the current mtime of every tracked file against the stored baseline, updating the
    // baseline as it goes. Returns true if any file's mtime changed since the last check.
    bool files_changed_on_disk();

    std::function<void()>              m_on_changed;
    wxFileSystemWatcher*               m_watcher{ nullptr };
    wxTimer                            m_debounce_timer;
    std::set<std::string>              m_watched_files;
    std::map<std::string, std::time_t> m_mtimes;
};

}} // namespace Slic3r::GUI

#endif // slic3r_GUI_SourceFileWatcher_hpp_
