import Foundation
import Darwin

/// Is something already listening on 127.0.0.1:<port>? Used so the app ADOPTS an
/// already-running engine/relay/dashboard (e.g. started from a terminal) instead of
/// launching a duplicate that would fight for the port and exit. Best-effort, instant.
enum PortCheck {
    static func inUse(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0   // connected → someone is listening
    }
}
