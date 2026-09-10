#ifndef slic3r_InstanceRegistry_hpp_
#define slic3r_InstanceRegistry_hpp_

#include <string>
#include <vector>
#include <optional>

namespace Slic3r { namespace GUI {

// Lets a running OrcaSlicer instance be addressed individually by an external trigger
// (--target-instance / --target-file), on top of the existing single-instance messaging that
// only ever addresses "the" instance for a given executable path. Each running instance
// publishes a small registry file under data_dir()/cache/instances/ so a short-lived CLI
// invocation can resolve a target to a specific notification channel before relaying its
// message, without needing to know anything about which build or window it's talking to.
//
// The registry file's own pid/exe_path/channel_id fields are written unconditionally; the
// loaded_files field is only included when the user has opted in via the
// "expose_loaded_files_for_targeting" preference (off by default) -- see the comment on
// update_loaded_files() for why that's separate from the rest of the registration.
class InstanceRegistry
{
public:
    // Registers this process in the registry with a freshly generated channel id, and returns
    // that id so the caller can also listen for messages sent to it. Safe to call only once per
    // process (from OtherInstanceMessageHandler::init()).
    static std::string register_instance();

    // Removes this process's registry entry. Safe to call even if register_instance() was never
    // called, or was already unregistered.
    static void unregister_instance();

    // Updates (or, if the preference is off, clears) the loaded_files field of this process's
    // registry entry. Writing the actual paths of everything currently open to a file that
    // persists on disk -- readable by other local accounts unless the OS/filesystem restricts
    // that further -- is a real, if modest, information disclosure some users won't want by
    // default (project/client names embedded in paths, etc.), so it's opt-in independently of
    // the base registration, which only ever reveals a pid and an executable path.
    static void update_loaded_files(const std::vector<std::string>& files);

    // Scans the registry (skipping entries whose process is no longer alive) for one matching
    // the given id against either its pid or its channel id, and returns its channel id.
    static std::optional<std::string> resolve_by_instance_id(const std::string& id);

    // Scans the registry (skipping stale entries, and entries with no loaded_files published)
    // for one whose loaded_files contains a path equivalent to the given one, and returns its
    // channel id.
    static std::optional<std::string> resolve_by_loaded_file(const std::string& path);
};

}} // namespace Slic3r::GUI

#endif // slic3r_InstanceRegistry_hpp_
