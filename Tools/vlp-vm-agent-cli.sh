#!/bin/bash
# version 1.1 29-May 2025

#AGENT_URL="https://content.labplatform.vmware.com/api/storage/file/NEE/agent/vlp-agent-"
AGENT_URL="https://content.labplatform.broadcom.com/api/storage/file/NEE/agent/vlp-agent-"

AGENT_DIR=vlp-agent
HR="-------------------------------------------------------------------------------------------------------------------------------"

HELP_TEXT="
Usage: vlp-vm-agent-cli [options]

Options:

  help - Shows help information

    $HR
    install (installs JRE, VLP Agent jar and builds start and stop scripts)
    $HR
    --platform (required) - The OS agent will be installed. Possible values [linux-arm64, linux-x64, macos-arm64, macos-x64]
    --version (optional) - The agent version . If not provided the latest agent will be installed

    Example:
    Install specific agent version
    ./vlp-vm-agent-cli.sh install --platform macos-x64 --version 1.0.1

    Install latest agent version
    ./vlp-vm-agent-cli.sh install --platform macos-x64

    $HR
    start (start VLP Agent jar. input is redirected to vlp-agent.log)
    $HR
    Example:
    ./vlp-vm-agent-cli.sh start

    $HR
    start (stop VLP Agent jar)
    $HR
    Example:
    ./vlp-vm-agent-cli.sh stop

"

# Function to display help information
show_help() {
    echo "$HELP_TEXT"
}

# Function to start the agent
operation_start() {
    echo ""
    cd $AGENT_DIR
    echo $HR
    echo "Starting VLP Agent."
    echo $HR
    ./vlp-agent-start.sh
}

# Function to start the agent
operation_stop() {
    echo $HR
    cd $AGENT_DIR
    echo "Stopping VLP Agent."
    echo $HR
    ./vlp-agent-stop.sh
}

# Function to install agent and JRE
operation_install() {
    echo ""
    if [ -z "$PLATFORM" ]; then
        echo "The parameter 'platform' is required. Type help for more information."
        exit 1
    fi

    if [ -z "$AGENT_VERSION" ]; then
        AGENT_VERSION="latest"
    fi

    case $PLATFORM in
        linux-arm64)
            JRE="https://download.java.net/java/GA/jdk23.0.1/c28985cbf10d4e648e4004050f8781aa/11/GPL/openjdk-23.0.1_linux-aarch64_bin.tar.gz"
            AGENT_JAVA_HOME=./jre/bin/java
            ;;

        linux-x64)
            JRE="https://download.java.net/java/GA/jdk23.0.1/c28985cbf10d4e648e4004050f8781aa/11/GPL/openjdk-23.0.1_linux-x64_bin.tar.gz"
            AGENT_JAVA_HOME=./jre/bin/java
            ;;

        macos-arm64)
            JRE="https://download.java.net/java/GA/jdk23.0.1/c28985cbf10d4e648e4004050f8781aa/11/GPL/openjdk-23.0.1_macos-aarch64_bin.tar.gz"
            AGENT_JAVA_HOME=./jre/Contents/Home/bin/java
            ;;

        macos-x64)
            JRE="https://download.java.net/java/GA/jdk23.0.1/c28985cbf10d4e648e4004050f8781aa/11/GPL/openjdk-23.0.1_macos-x64_bin.tar.gz"
            AGENT_JAVA_HOME=./jre/Contents/Home/bin/java
            ;;

        *)
            echo "The parameter 'platform' contains invalid value. $PLATFORM"
            exit 1
            ;;
    esac

    # Create directory for agent files if doesn't exist
    echo "Creating agent directory $AGENT_DIR"
    if [ ! -d $AGENT_DIR ]; then
        mkdir $AGENT_DIR
    else
        echo "Directory $AGENT_DIR already exist. Skipping creating directory."
    fi
    echo $HR

    echo "Entering directory $AGENT_DIR."
    cd $AGENT_DIR
    echo $HR

    # Download JRE if doesn't exist
    echo "Downloading Java Runtime 23"
    if [ ! -d jre ]; then
        curl -o jdk.tar.gz $JRE
        tar -xf jdk.tar.gz
        mv jdk-23* jre
        rm -rf jdk.tar.gz
    else
        echo "Java Runtime 23 already exist. Skipping download."
    fi
    echo $HR

    AGENT_NAME=vlp-agent-$AGENT_VERSION.jar

    # If no agent version is provided remove 'latest' version to ensure it will always get updated
    if [ $AGENT_VERSION == "latest" ]; then
        if [ -f $AGENT_NAME ]; then
            rm -rf $AGENT_NAME
        fi
    fi

    # Download JAR file if it doesn't exist
    echo "Downloading Agent version $AGENT_VERSION"
    if [ ! -f "$AGENT_NAME" ]; then
        url="${AGENT_URL}${AGENT_VERSION}.jar"
        attempt=1
        success=false

        while [ $attempt -le 10 ]; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt: Checking $url"
            
            # Run HEAD request and capture both exit code and stderr
            output=$(curl --silent --head --fail "$url" 2>&1 > /dev/null)
            status=$?

            if [ $status -eq 0 ]; then
                echo "URL is reachable. Downloading..."
                curl -o "$AGENT_NAME" "$url"
                chmod +x "$AGENT_NAME"
                success=true
                break
            else
                echo "Attempt $attempt failed (exit code $status). Error: $output"
                echo "Retrying in 10 seconds..."
                sleep 10
            fi

            attempt=$((attempt+1))
        done

        if [ "$success" = false ]; then
            echo "Agent version not found after $attempt-1 attempts: $AGENT_VERSION"
            exit 1
        fi
    else
        echo "Agent version $AGENT_VERSION already exists. Skipping download."
    fi

    echo $HR

    if [ $PLATFORM == "linux*" ]; then
        START_COMMAND="$AGENT_JAVA_HOME -jar $AGENT_NAME --spring.profiles.active=prod --logging.level.com.vmwlp.egwagent=DEBUG 2>&1 > "vlp-agent.log"  &"
        STOP_COMMAND="kill -9 $(pidof java | grep -F $AGENT_NAME)"
    else
        START_COMMAND="nohup $AGENT_JAVA_HOME -jar $AGENT_NAME --spring.profiles.active=prod --logging.level.com.vmwlp.egwagent=DEBUG 2>&1 > "vlp-agent.log"  &"
        STOP_COMMAND="pkill -f 'java -jar $AGENT_NAME'"
    fi

    echo " #!/bin/bash
    $START_COMMAND" > "vlp-agent-start.sh"
    chmod +x vlp-agent-start.sh

    echo " #!/bin/bash
    $STOP_COMMAND" > "vlp-agent-stop.sh"
    chmod +x vlp-agent-stop.sh

echo "$HR
Installation finished.
To start the agent use ./vlp-vm-agent-cli.sh start
To stop the agent use ./vlp-vm-agent-cli.sh stop
Input is redirected to ./$AGENT_DIR/vlp-agent.log file.
$HR"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform)
            PLATFORM="$2"
            shift 2
            ;;
        --version)
            AGENT_VERSION="$2"
            shift 2
            ;;
        help | install | start | stop)
            OPERATION="$1"
            shift 1
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

case $OPERATION in
  help)
    show_help
    ;;
  install)
    operation_install
    ;;
  start)
    operation_start
    ;;
  stop)
    operation_stop
    ;;
  *)
    echo -n "unknown operation"
    exit 1
    ;;
esac
