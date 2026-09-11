#include "InstanceRegistry.hpp"
#include "GUI_App.hpp"

#include "libslic3r/Utils.hpp"

#include <wx/stdpaths.h>

#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>
#include <boost/uuid/uuid.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <boost/uuid/uuid_io.hpp>
#include <boost/nowide/fstream.hpp>

#include <nlohmann/json.hpp>

#ifndef _WIN32
#include <unistd.h>
#include <signal.h>
#endif

namespace fs = boost::filesystem;
using json = nlohmann::json;

namespace Slic3r { namespace GUI {

namespace {

#ifndef _WIN32

    long current_pid() { return static_cast<long>(getpid()); }

    bool pid_is_alive(long pid)
    {
        // Signal 0 sends nothing but still performs the existence/permission check; since every
        // instance we ever register belongs to the current user, EPERM shouldn't happen here in
        // practice, but treat anything other than "no such process" as "still alive" to be safe.
        return pid > 0 && (kill(static_cast<pid_t>(pid), 0) == 0 || errno != ESRCH);
    }

    fs::path registry_dir() { return fs::path(data_dir()) / "cache" / "instances"; }

    fs::path registry_path_for(long pid) { return registry_dir() / (std::to_string(pid) + ".json"); }

    // The channel id and this process's own pid, kept for the lifetime of the process once
    // register_instance() is called, so unregister_instance()/update_loaded_files() don't need
    // to re-derive or be passed them.
    std::string s_channel_id;
    bool        s_registered = false;

    void write_registry_file(const std::vector<std::string>& loaded_files)
    {
        json j;
        j["pid"] = current_pid();
        j["exe_path"] = fs::system_complete(wxStandardPaths::Get().GetExecutablePath().ToUTF8().data()).string();
        j["channel_id"] = s_channel_id;
        if (wxGetApp().app_config->get_bool("expose_loaded_files_for_targeting") && !loaded_files.empty()) {
            // Canonical so a --target-file lookup (which canonicalises its argument) matches
            // regardless of symlinks in either path, e.g. /tmp vs. /private/tmp on macOS.
            std::vector<std::string> canonical_files;
            for (const std::string& file : loaded_files) {
                boost::system::error_code ec;
                const fs::path canonical = fs::canonical(file, ec);
                canonical_files.push_back(ec ? file : canonical.string());
            }
            j["loaded_files"] = canonical_files;
        }

        boost::system::error_code ec;
        fs::create_directories(registry_dir(), ec);

        const fs::path path = registry_path_for(current_pid());
        boost::nowide::ofstream file(path.string(), std::ios::trunc);
        if (!file.good()) {
            BOOST_LOG_TRIVIAL(warning) << "InstanceRegistry: failed to open " << path.string() << " for writing";
            return;
        }
        file << j.dump();
        file.close();

        // Owner-only: this file can list every path currently open in this instance, which
        // other local accounts on a shared machine have no business reading. Framework file
        // creation typically yields 0644; tighten it explicitly rather than relying on umask.
        fs::permissions(path, fs::owner_read | fs::owner_write, ec);
    }

    // Reads one registry file, returning nullopt for anything unreadable, malformed, or stale
    // (pid no longer alive -- e.g. a crashed instance that never got to clean up after itself).
    // A definitively stale entry (valid JSON, dead pid) is deleted on the spot rather than just
    // skipped: this is the only case we can be sure about without risking a live file, so it's
    // safe to prune opportunistically instead of leaving it to accumulate forever. A malformed or
    // unreadable file is left alone -- it could be another instance's write in progress -- and
    // will just get retried (or eventually cleaned up by prune_stale_entries() below) later.
    std::optional<json> read_live_entry(const fs::path& path)
    {
        boost::nowide::ifstream file(path.string());
        if (!file.good())
            return std::nullopt;
        json j;
        try {
            file >> j;
        } catch (const std::exception&) {
            return std::nullopt;
        }
        if (!j.contains("pid") || !j["pid"].is_number_integer() ||
            !j.contains("channel_id") || !j["channel_id"].is_string())
            return std::nullopt;
        if (!pid_is_alive(j["pid"].get<long>())) {
            boost::system::error_code ec;
            fs::remove(path, ec);
            return std::nullopt;
        }
        return j;
    }

    template<typename Predicate>
    std::optional<std::string> find_channel_if(Predicate&& matches)
    {
        boost::system::error_code ec;
        if (!fs::is_directory(registry_dir(), ec))
            return std::nullopt;

        for (const auto& entry : fs::directory_iterator(registry_dir())) {
            if (entry.path().extension() != ".json")
                continue;
            std::optional<json> j = read_live_entry(entry.path());
            try {
                if (j.has_value() && matches(*j))
                    return j->at("channel_id").get<std::string>();
            } catch (const std::exception&) {
                // A field with an unexpected type in one entry shouldn't abort the whole lookup.
            }
        }
        return std::nullopt;
    }

    // Sweeps the whole registry for stale entries left behind by instances that didn't exit
    // cleanly (a crash, a force-quit, a killed process -- none of which run
    // InstanceRegistry::unregister_instance()). Run once per launch (from register_instance()) so
    // the registry doesn't just grow forever between clean shutdowns; find_channel_if() above
    // also prunes opportunistically as a side effect of any --target-instance/--target-file
    // lookup, but a fresh launch is a much more reliable, regularly-occurring trigger than that.
    void prune_stale_entries()
    {
        boost::system::error_code ec;
        if (!fs::is_directory(registry_dir(), ec))
            return;
        for (const auto& entry : fs::directory_iterator(registry_dir()))
            if (entry.path().extension() == ".json")
                read_live_entry(entry.path()); // discards the result; only the deletion side effect matters here
    }

#endif // !_WIN32

} // anonymous namespace

std::string InstanceRegistry::register_instance()
{
#ifndef _WIN32
    prune_stale_entries();
    s_channel_id = boost::uuids::to_string(boost::uuids::random_generator()());
    s_registered = true;
    write_registry_file({});
    return s_channel_id;
#else
    return std::string();
#endif
}

void InstanceRegistry::unregister_instance()
{
#ifndef _WIN32
    if (!s_registered)
        return;
    boost::system::error_code ec;
    fs::remove(registry_path_for(current_pid()), ec);
    s_registered = false;
#endif
}

void InstanceRegistry::update_loaded_files(const std::vector<std::string>& files)
{
#ifndef _WIN32
    if (!s_registered)
        return;
    write_registry_file(files);
#endif
}

std::optional<std::string> InstanceRegistry::resolve_by_instance_id(const std::string& id)
{
#ifndef _WIN32
    return find_channel_if([&id](const json& j) {
        return j.at("channel_id").get<std::string>() == id ||
               std::to_string(j.at("pid").get<long>()) == id;
    });
#else
    return std::nullopt;
#endif
}

std::optional<std::string> InstanceRegistry::resolve_by_loaded_file(const std::string& path)
{
#ifndef _WIN32
    boost::system::error_code ec;
    const fs::path target = fs::canonical(path, ec);
    const std::string target_str = ec ? path : target.string();

    return find_channel_if([&target_str](const json& j) {
        if (!j.contains("loaded_files"))
            return false;
        for (const auto& file : j.at("loaded_files"))
            if (file.get<std::string>() == target_str)
                return true;
        return false;
    });
#else
    return std::nullopt;
#endif
}

}} // namespace Slic3r::GUI
