import Foundation
import AVFoundation
import CoreMedia

// Read-only asset loading. No AVPlayer, player app, settings or audio device API.
guard CommandLine.arguments.count == 2 else { exit(2) }
let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
func fourcc(_ value: FourCharCode) -> String {
    let bytes = [UInt8((value >> 24) & 255), UInt8((value >> 16) & 255),
                 UInt8((value >> 8) & 255), UInt8(value & 255)]
    return String(bytes: bytes, encoding: .ascii) ?? ""
}
Task {
    do {
        let playable = try await asset.load(.isPlayable)
        let duration = try await asset.load(.duration)
        var tracks: [[String: Any]] = []
        for mediaType in [AVMediaType.video, AVMediaType.audio] {
            for track in try await asset.loadTracks(withMediaType: mediaType) {
                let descriptions = try await track.load(.formatDescriptions)
                let language = try await track.load(.languageCode) ?? ""
                tracks.append(["type": mediaType.rawValue, "language": language,
                    "formats": descriptions.map { format -> [String: Any] in
                        ["codec": fourcc(CMFormatDescriptionGetMediaSubType(format)),
                         "extensions": String(describing: CMFormatDescriptionGetExtensions(format))]
                    }])
            }
        }
        let result: [String: Any] = ["is_playable": playable, "duration_seconds": CMTimeGetSeconds(duration),
                                    "tracks": tracks, "playback_started": false]
        print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
        exit(0)
    } catch {
        fputs("Native asset inspection failed.\n", stderr)
        exit(1)
    }
}
dispatchMain()
