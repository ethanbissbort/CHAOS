using Chaos.Api;

// ---------------------------------------------------------------------------
// Conformance host: composes Chaos.Api over one SQLite database and serves it.
//
// Everything here is what Chaos.Host will do in production, minus the reverse
// proxy, the supervisor and the route-ownership manifest. If this file needs
// more than AddChaosApi/MapChaosApi to stand the endpoints up, the library's
// registration surface is wrong and the library is what should change.
//
//   dotnet run --project Chaos.Api.ConformanceHost -- \
//       --database <path-to.db> --urls http://127.0.0.1:0
//
// Port 0 asks the OS for a free port; the chosen address is printed on stdout
// as "CHAOS_CONFORMANCE_LISTENING <url>" so the harness never hard-codes one.
// ---------------------------------------------------------------------------

var builder = WebApplication.CreateSlimBuilder(args);

var databasePath = GetArgument(args, "--database")
    ?? Environment.GetEnvironmentVariable("CHAOS_API_SQLITE_PATH");

if (string.IsNullOrWhiteSpace(databasePath))
{
    await Console.Error.WriteLineAsync(
        "usage: Chaos.Api.ConformanceHost --database <path-to-sqlite.db> [--urls <url>]")
        .ConfigureAwait(false);
    return 2;
}

if (!File.Exists(databasePath))
{
    await Console.Error.WriteLineAsync($"database not found: {databasePath}").ConfigureAwait(false);
    return 2;
}

builder.Logging.SetMinimumLevel(LogLevel.Warning);
builder.Services.AddChaosApi(options => options.SqliteDatabasePath = databasePath);

var app = builder.Build();
app.MapChaosApi();

await app.StartAsync().ConfigureAwait(false);

// Announce the real bound address, including the OS-assigned port. The harness
// blocks on this line rather than polling a guessed port.
var addresses = app.Services
    .GetRequiredService<Microsoft.AspNetCore.Hosting.Server.IServer>()
    .Features.Get<Microsoft.AspNetCore.Hosting.Server.Features.IServerAddressesFeature>()
    ?.Addresses ?? [];

foreach (var address in addresses)
{
    Console.WriteLine($"CHAOS_CONFORMANCE_LISTENING {address}");
}

Console.Out.Flush();

await app.WaitForShutdownAsync().ConfigureAwait(false);
return 0;

static string? GetArgument(string[] args, string name)
{
    for (var index = 0; index < args.Length - 1; index++)
    {
        if (string.Equals(args[index], name, StringComparison.Ordinal))
        {
            return args[index + 1];
        }
    }

    return null;
}
