using Chaos.Host.Supervisor.Runtime;
using Chaos.Host.Supervisor.Tests.Fakes;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// Runtime resolution, including the case that matters most: a packaged install
/// whose embedded interpreter has gone missing must fail loudly rather than
/// quietly borrow whatever Python the machine happens to have.
/// </summary>
public sealed class PythonRuntimeResolverTests
{
    // Normalised through Path() like every other path here. Left unnormalised
    // these are POSIX roots, and on Windows the resolver then joins a "/"-rooted
    // string to "\"-separated tails: "/opt/chaos\python\bin\python3". The
    // resolver is right and the expectation was wrong.
    private static readonly string InstallRoot = Path("/opt/chaos");
    private static readonly string RepositoryRoot = Path("/home/dev/homestead-twin");

    [Fact]
    public void The_embedded_runtime_wins_when_it_is_present()
    {
        var files = new FakeFileSystem()
            .AddDirectory(Path("/opt/chaos/python"))
            .AddFile(Path("/opt/chaos/python/bin/python3"))
            .AddFile(Path("/opt/chaos/app/src/homestead_twin/cli.py"))
            .AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions { InstallRoot = InstallRoot });

        Assert.True(resolution.Succeeded);
        Assert.Equal(BackendRuntimeLayout.Embedded, resolution.Runtime!.Layout);
        Assert.Equal(Path("/opt/chaos/python/bin/python3"), resolution.Runtime.Executable);
        Assert.Equal(Path("/opt/chaos/app/src"), resolution.Runtime.Environment["PYTHONPATH"]);
        Assert.Contains("EMBEDDED", resolution.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void On_windows_the_embedded_runtime_is_python_exe_under_the_install_root()
    {
        var files = new FakeFileSystem()
            .AddDirectory(Path(@"C:\Program Files\CHAOS\python"))
            .AddFile(Path(@"C:\Program Files\CHAOS\python\python.exe"));

        // InstallRoot goes through Path() like every other path in this test:
        // unnormalised, the resolver joins a Windows-separated root to a
        // host-separated tail and the assertion fails on Linux for a reason
        // that has nothing to do with the contract under test.
        var resolution = new PythonRuntimeResolver(files, isWindows: true)
            .Resolve(new BackendSupervisorOptions { InstallRoot = Path(@"C:\Program Files\CHAOS") });

        Assert.True(resolution.Succeeded);
        Assert.Equal(BackendRuntimeLayout.Embedded, resolution.Runtime!.Layout);
        Assert.Equal(Path(@"C:\Program Files\CHAOS\python\python.exe"), resolution.Runtime.Executable);
    }

    [Fact]
    public void A_broken_embedded_install_fails_instead_of_falling_back_to_system_python()
    {
        // The runtime directory is there; the interpreter is not. That is a
        // damaged install, and a system Python would run the platform against
        // unknown dependency versions while looking healthy.
        var files = new FakeFileSystem()
            .AddDirectory(Path("/opt/chaos/python"))
            .AddFile(Path("/home/dev/homestead-twin/src/homestead_twin/cli.py"))
            .AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = InstallRoot,
                RepositoryRoot = RepositoryRoot,
            });

        Assert.False(resolution.Succeeded);
        Assert.Null(resolution.Runtime);
        Assert.Contains("damaged", resolution.Detail, StringComparison.Ordinal);
        Assert.Contains("will not fall back to a system Python", resolution.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void With_no_embedded_directory_at_all_the_development_layout_is_used()
    {
        var files = new FakeFileSystem()
            .AddFile(Path("/home/dev/homestead-twin/src/homestead_twin/cli.py"))
            .AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = Path("/home/dev/homestead-twin/windows/src/Chaos.Host/bin"),
            });

        Assert.True(resolution.Succeeded);
        Assert.Equal(BackendRuntimeLayout.Development, resolution.Runtime!.Layout);
        Assert.Equal("/usr/bin/python3", resolution.Runtime.Executable);
        Assert.Equal(Path("/home/dev/homestead-twin/src"), resolution.Runtime.Environment["PYTHONPATH"]);
        Assert.Equal(Path("/home/dev/homestead-twin"), resolution.Runtime.WorkingDirectory);

        // The trail says why, so "which Python am I actually running?" is never
        // a guess.
        Assert.Contains(
            resolution.Attempts,
            attempt => attempt.Contains("this is not a packaged install", StringComparison.Ordinal));
    }

    [Fact]
    public void RequireEmbeddedRuntime_refuses_to_consider_the_development_layout()
    {
        var files = new FakeFileSystem()
            .AddFile(Path("/home/dev/homestead-twin/src/homestead_twin/cli.py"))
            .AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = InstallRoot,
                RepositoryRoot = RepositoryRoot,
                RequireEmbeddedRuntime = true,
            });

        Assert.False(resolution.Succeeded);
        Assert.Contains("incomplete", resolution.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void A_configured_repository_root_without_the_platform_is_an_error_not_a_search()
    {
        var files = new FakeFileSystem().AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = InstallRoot,
                RepositoryRoot = Path("/wrong/place"),
            });

        Assert.False(resolution.Succeeded);
        Assert.Contains("no search was made", resolution.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public void No_interpreter_anywhere_is_reported_with_every_place_that_was_tried()
    {
        var files = new FakeFileSystem()
            .AddFile(Path("/home/dev/homestead-twin/src/homestead_twin/cli.py"));

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = InstallRoot,
                RepositoryRoot = RepositoryRoot,
            });

        Assert.False(resolution.Succeeded);
        Assert.Contains("No Python interpreter on PATH", resolution.Detail, StringComparison.Ordinal);
        Assert.Contains(resolution.Attempts, attempt => attempt.Contains("'python3' not on PATH", StringComparison.Ordinal));
        Assert.Contains(resolution.Attempts, attempt => attempt.Contains("'python' not on PATH", StringComparison.Ordinal));
        Assert.Contains("- ", resolution.ToReport(), StringComparison.Ordinal);
    }

    [Fact]
    public void An_explicitly_configured_interpreter_that_does_not_exist_is_not_silently_replaced()
    {
        var files = new FakeFileSystem()
            .AddFile(Path("/home/dev/homestead-twin/src/homestead_twin/cli.py"))
            .AddOnPath("python3", "/usr/bin/python3");

        var resolution = new PythonRuntimeResolver(files, isWindows: false)
            .Resolve(new BackendSupervisorOptions
            {
                InstallRoot = InstallRoot,
                RepositoryRoot = RepositoryRoot,
                PythonExecutable = "/opt/custom/python3.13",
            });

        Assert.False(resolution.Succeeded);
        Assert.Contains("no other interpreter was tried", resolution.Detail, StringComparison.Ordinal);
    }

    /// <summary>Makes the literals in this file work on both path conventions.</summary>
    private static string Path(string path) =>
        path.Replace('/', System.IO.Path.DirectorySeparatorChar)
            .Replace('\\', System.IO.Path.DirectorySeparatorChar);
}
