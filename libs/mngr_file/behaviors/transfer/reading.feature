Feature: Reading a file from a machine
  A read takes one addressed file and delivers its bytes locally, either onto the local output stream or into a named local file.

  @bytes-to-output
  Scenario: A read with no named destination puts the file's bytes on the output stream
    Given an addressed file holding known content
    When it is read with no local destination named and output rendered for a human reader
    Then exactly that file's bytes appear on the local output stream
    And nothing else is written to the output stream

  @content-in-machine-readable-report
  Scenario: A machine-readable read carries the content as encoded bytes
    Rendering content as text would destroy any file that is not text, so the record carries an encoding that survives arbitrary bytes.

    Given an addressed file holding content that is not valid text
    When it is read with no local destination named and output rendered machine-readably
    Then the record carries the file's exact bytes in encoded form
    And the record carries the file's size and the absolute path read

  @saved-to-local-file
  Scenario: A read saved to a named local file reports the save instead of the content
    Given an addressed file holding known content
    When it is read with a local destination named
    Then the local file holds exactly the addressed file's bytes
    And the command reports the size, the absolute path read, and the local path written
    And the report does not repeat the file's content

  @local-parents-created
  Scenario: Saving to a local path creates the local directories leading to it
    Given an addressed file holding known content
    When it is read with a local destination whose parent directories do not exist
    Then those directories are created
    And the local file holds exactly the addressed file's bytes

  @no-file-at-path
  Scenario: Reading a path that holds nothing is refused
    When a read addresses a path where no file exists
    Then the command refuses and names the absolute path it could not read
    And it points the user at the means of discovering what the directory does hold

  @path-is-a-directory
  Scenario: Reading a path that names a directory is refused
    A directory has no single content to deliver, and a user reaching for one almost certainly means to transfer the whole of it, which is a different command's job.

    When a read addresses a path that names a directory
    Then the command refuses and reports that the path is a directory rather than a file
    And it points the user at the means of transferring a directory
    And it points the user at the means of seeing what the directory holds

  @directory-refusal-is-machine-independent
  Scenario: A directory is refused the same way wherever the file lives
    The refusal is a fact about the path, so it cannot depend on how the machine holding it happens to be reached.

    Given two targets whose machines are reached by different means
    When a read addresses a path that names a directory on each
    Then both commands refuse in the same terms
