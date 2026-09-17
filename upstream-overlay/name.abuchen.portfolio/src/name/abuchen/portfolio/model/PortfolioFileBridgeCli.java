/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: EPL-1.0
 */
package name.abuchen.portfolio.model;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.EnumSet;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

import org.eclipse.core.runtime.NullProgressMonitor;

/**
 * Privacy-safe official format detector and normalizer for the Windows bundle.
 */
public final class PortfolioFileBridgeCli
{
    private static final byte[] ENCRYPTED_SIGNATURE = "PORTFOLIO".getBytes(StandardCharsets.US_ASCII); //$NON-NLS-1$
    private static final byte[] ZIP_SIGNATURE = { 'P', 'K', 3, 4 };
    private static final long MAX_SOURCE_BYTES = 128L * 1024L * 1024L;
    private static final long MAX_NORMALIZED_BYTES = 256L * 1024L * 1024L;
    private static final long MAX_EXPANSION_RATIO = 100L;

    private enum Kind
    {
        XML, XML_ID, XML_ZIP, BINARY, ENCRYPTED, EMPTY, UNSUPPORTED,
        ENCRYPTED_HEADER, UNSUPPORTED_AES, DAMAGED_ZIP
    }

    private PortfolioFileBridgeCli()
    {
    }

    public static void main(String[] args)
    {
        int code = execute(args);
        if (code != 0)
            System.exit(code);
    }

    private static int execute(String[] args)
    {
        if (args.length == 2 && "inspect".equals(args[0])) //$NON-NLS-1$
            return inspect(Path.of(args[1]));
        if (args.length == 3 && "normalize".equals(args[0])) //$NON-NLS-1$
            return normalize(Path.of(args[1]), Path.of(args[2]));
        status("FAILED", "E_USAGE", "UNAVAILABLE", "UNAVAILABLE", null, null, null); //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$
        return 64;
    }

    private static int inspect(Path input)
    {
        try
        {
            Kind kind = detect(input);
            if (kind == Kind.EMPTY)
                return failed("E_EMPTY", kind); //$NON-NLS-1$
            if (kind == Kind.UNSUPPORTED)
                return failed("E_UNSUPPORTED_FORMAT", kind); //$NON-NLS-1$
            if (kind == Kind.ENCRYPTED_HEADER)
                return failed("E_ENCRYPTED_HEADER", kind); //$NON-NLS-1$
            if (kind == Kind.UNSUPPORTED_AES)
                return failed("E_AES_METHOD_UNSUPPORTED", kind); //$NON-NLS-1$
            if (kind == Kind.DAMAGED_ZIP)
                return failed("E_ZIP_CORRUPT", kind); //$NON-NLS-1$
            status("OK", "OK", kind.name(), body(kind, null), sha256(input), null, null); //$NON-NLS-1$ //$NON-NLS-2$
            return 0;
        }
        catch (IOException e)
        {
            return failed("E_INPUT_READ", Kind.UNSUPPORTED); //$NON-NLS-1$
        }
    }

    private static int normalize(Path input, Path output)
    {
        char[] password = null;
        String sourceHash = null;
        PrintStream originalOut = System.out;
        PrintStream originalErr = System.err;
        PrintStream sink = new PrintStream(OutputStream.nullOutputStream());
        try
        {
            if (!Files.isRegularFile(input) || Files.isSymbolicLink(input))
                return failed("E_INPUT_READ", Kind.UNSUPPORTED); //$NON-NLS-1$
            long sourceBytes = Files.size(input);
            if (sourceBytes > MAX_SOURCE_BYTES)
                return failed("E_SOURCE_TOO_LARGE", Kind.UNSUPPORTED); //$NON-NLS-1$
            Kind kind = detect(input);
            if (kind == Kind.EMPTY)
                return failed("E_EMPTY", kind); //$NON-NLS-1$
            if (kind == Kind.UNSUPPORTED)
                return failed("E_UNSUPPORTED_FORMAT", kind); //$NON-NLS-1$
            if (kind == Kind.ENCRYPTED_HEADER)
                return failed("E_ENCRYPTED_HEADER", kind); //$NON-NLS-1$
            if (kind == Kind.UNSUPPORTED_AES)
                return failed("E_AES_METHOD_UNSUPPORTED", kind); //$NON-NLS-1$
            if (kind == Kind.DAMAGED_ZIP)
                return failed("E_ZIP_CORRUPT", kind); //$NON-NLS-1$

            sourceHash = sha256(input);
            if (kind == Kind.ENCRYPTED)
            {
                password = readPassword();
                if (password == null || password.length == 0)
                    return failed("E_PASSWORD_MISSING", kind); //$NON-NLS-1$
            }

            System.setOut(sink);
            System.setErr(sink);
            Client client;
            try
            {
                client = ClientFactory.load(input.toFile(), password, new NullProgressMonitor());
            }
            catch (IOException e)
            {
                System.setOut(originalOut);
                System.setErr(originalErr);
                String failure = kind == Kind.ENCRYPTED ? "E_PASSWORD_INCORRECT" //$NON-NLS-1$
                                : kind == Kind.BINARY ? "E_PROTOBUF_CORRUPT" //$NON-NLS-1$
                                : kind == Kind.XML_ZIP ? "E_ZIP_CORRUPT" //$NON-NLS-1$
                                : "E_DECODE_FAILED"; //$NON-NLS-1$
                return failed(failure, kind);
            }
            Set<SaveFlag> loadedFlags = EnumSet.copyOf(client.getSaveFlags());
            ClientFactory.exportAs(client, output.toFile(), null, EnumSet.of(SaveFlag.XML));
            System.setOut(originalOut);
            System.setErr(originalErr);

            long normalizedBytes = Files.size(output);
            if (normalizedBytes > MAX_NORMALIZED_BYTES)
            {
                Files.deleteIfExists(output);
                return failed("E_NORMALIZED_TOO_LARGE", kind); //$NON-NLS-1$
            }
            if (sourceBytes > 0 && normalizedBytes > sourceBytes * MAX_EXPANSION_RATIO)
            {
                Files.deleteIfExists(output);
                return failed("E_EXPANSION_LIMIT", kind); //$NON-NLS-1$
            }
            if (!sourceHash.equals(sha256(input)))
            {
                Files.deleteIfExists(output);
                return failed("E_SOURCE_CHANGED", kind); //$NON-NLS-1$
            }
            try
            {
                Files.setPosixFilePermissions(output, java.nio.file.attribute.PosixFilePermissions.fromString("rw-------")); //$NON-NLS-1$
            }
            catch (UnsupportedOperationException ignored)
            {
                // Windows ACLs are owned by the private parent directory.
            }
            String body = loadedFlags.contains(SaveFlag.BINARY) ? "BINARY" : "XML"; //$NON-NLS-1$ //$NON-NLS-2$
            status("OK", "OK", kind.name(), body, sourceHash, sha256(output), normalizedBytes); //$NON-NLS-1$ //$NON-NLS-2$
            return 0;
        }
        catch (IOException e)
        {
            System.setOut(originalOut);
            System.setErr(originalErr);
            try
            {
                Files.deleteIfExists(output);
            }
            catch (IOException ignored)
            {
                // best effort; the Python owner removes the private directory
            }
            return failed("E_INPUT_READ", Kind.UNSUPPORTED); //$NON-NLS-1$
        }
        finally
        {
            System.setOut(originalOut);
            System.setErr(originalErr);
            sink.close();
            if (password != null)
                Arrays.fill(password, '\0');
        }
    }

    private static char[] readPassword() throws IOException
    {
        String line = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8)).readLine();
        return line == null ? null : line.toCharArray();
    }

    private static Kind detect(Path input) throws IOException
    {
        long size = Files.size(input);
        if (size == 0)
            return Kind.EMPTY;
        byte[] head = new byte[(int) Math.min(size, 512L)];
        try (var stream = Files.newInputStream(input, StandardOpenOption.READ))
        {
            int offset = 0;
            while (offset < head.length)
            {
                int read = stream.read(head, offset, head.length - offset);
                if (read < 0)
                    break;
                offset += read;
            }
        }
        if (startsWith(head, ENCRYPTED_SIGNATURE))
        {
            if (size < 42 || head.length < ENCRYPTED_SIGNATURE.length + 1)
                return Kind.ENCRYPTED_HEADER;
            int method = head[ENCRYPTED_SIGNATURE.length] & 0xff;
            if (method != 0 && method != 1)
                return Kind.UNSUPPORTED_AES;
            return Kind.ENCRYPTED;
        }
        if (startsWith(head, ZIP_SIGNATURE))
            return inspectZip(input);

        int offset = 0;
        if (head.length >= 3 && (head[0] & 0xff) == 0xef && (head[1] & 0xff) == 0xbb
                        && (head[2] & 0xff) == 0xbf)
            offset = 3;
        while (offset < head.length && Character.isWhitespace((char) (head[offset] & 0xff)))
            offset++;
        if (offset < head.length && head[offset] == '<')
        {
            String prefix = new String(head, offset, head.length - offset, StandardCharsets.UTF_8);
            return prefix.contains("<client id=") ? Kind.XML_ID : Kind.XML; //$NON-NLS-1$
        }
        return Kind.UNSUPPORTED;
    }

    private static Kind inspectZip(Path input)
    {
        try (ZipInputStream zip = new ZipInputStream(Files.newInputStream(input)))
        {
            ZipEntry entry = zip.getNextEntry();
            if (entry == null || entry.isDirectory())
                return Kind.DAMAGED_ZIP;
            return entry.getName().endsWith(".portfolio") ? Kind.BINARY : Kind.XML_ZIP; //$NON-NLS-1$
        }
        catch (IOException e)
        {
            return Kind.DAMAGED_ZIP;
        }
    }

    private static boolean startsWith(byte[] actual, byte[] expected)
    {
        if (actual.length < expected.length)
            return false;
        for (int index = 0; index < expected.length; index++)
            if (actual[index] != expected[index])
                return false;
        return true;
    }

    private static String body(Kind kind, Set<SaveFlag> flags)
    {
        if (flags != null && flags.contains(SaveFlag.BINARY))
            return "BINARY"; //$NON-NLS-1$
        if (kind == Kind.BINARY)
            return "BINARY"; //$NON-NLS-1$
        if (kind == Kind.ENCRYPTED || kind == Kind.ENCRYPTED_HEADER || kind == Kind.UNSUPPORTED_AES)
            return "ENCRYPTED"; //$NON-NLS-1$
        return "XML"; //$NON-NLS-1$
    }

    private static int failed(String code, Kind kind)
    {
        status("FAILED", code, kind.name(), body(kind, null), null, null, null); //$NON-NLS-1$
        return 65;
    }

    private static void status(String status, String code, String format, String body, String sourceHash,
                    String normalizedHash, Long normalizedBytes)
    {
        StringBuilder line = new StringBuilder();
        line.append("STATUS=").append(status).append("|CODE=").append(code) //$NON-NLS-1$ //$NON-NLS-2$
                        .append("|FORMAT=").append(format).append("|BODY=").append(body); //$NON-NLS-1$ //$NON-NLS-2$
        if (sourceHash != null)
            line.append("|SOURCE_SHA256=").append(sourceHash); //$NON-NLS-1$
        if (normalizedHash != null)
            line.append("|NORMALIZED_SHA256=").append(normalizedHash); //$NON-NLS-1$
        if (normalizedBytes != null)
            line.append("|NORMALIZED_BYTES=").append(normalizedBytes); //$NON-NLS-1$
        System.out.println(line);
    }

    private static String sha256(Path path) throws IOException
    {
        try
        {
            MessageDigest digest = MessageDigest.getInstance("SHA-256"); //$NON-NLS-1$
            try (var stream = Files.newInputStream(path))
            {
                byte[] buffer = new byte[65536];
                for (int read = stream.read(buffer); read >= 0; read = stream.read(buffer))
                    if (read > 0)
                        digest.update(buffer, 0, read);
            }
            StringBuilder value = new StringBuilder();
            for (byte item : digest.digest())
                value.append(String.format("%02x", item)); //$NON-NLS-1$
            return value.toString();
        }
        catch (java.security.NoSuchAlgorithmException e)
        {
            throw new IllegalStateException(e);
        }
    }
}
