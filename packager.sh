#!/bin/bash
#
# packager.sh  Copyright (C) 2014-2017 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

#
# DESCRIPTION
#
# packager.sh automates procedures to:
#  - test sources
#  - build Ubuntu package (official or development version)
#  - test Ubuntu package
#
# tests are performed inside linux containers (lxc) to achieve
# a good compromise between speed and isolation
#
# all lxc instances are ephemeral
#
# ephemeral containers are "clones" of a base container and have a
# temporary file system that reflects the contents of the base container
# but any modifications are stored in an overlayed, in-memory
# file system
#

if [ -n "$GEM_SET_DEBUG" -a "$GEM_SET_DEBUG" != "false" ]; then
    export PS4='+${BASH_SOURCE}:${LINENO}:${FUNCNAME[0]}: '
    set -x
fi
set -e
GEM_GIT_REPO="git://github.com/gem"
GEM_GIT_PACKAGE="oq-hazardlib"
GEM_DEPENDS="oq-libs|deb oq-libs-extra|sub"
GEM_DEB_PACKAGE="python-${GEM_GIT_PACKAGE}"
GEM_DEB_SERIE="master"
if [ -z "$GEM_DEB_REPO" ]; then
    GEM_DEB_REPO="$HOME/gem_ubuntu_repo"
fi
if [ -z "$GEM_DEB_MONOTONE" ]; then
    GEM_DEB_MONOTONE="$HOME/monotone"
fi
# FIXME this is currently unused, but left as reference
if [ "$GEM_EPHEM_USER" = "" ]; then
    GEM_EPHEM_USER="ubuntu"
fi

GEM_BUILD_ROOT="build-deb"
GEM_BUILD_SRC="${GEM_BUILD_ROOT}/${GEM_DEB_PACKAGE}"

GEM_ALWAYS_YES=false

if [ "$GEM_EPHEM_CMD" = "" ]; then
    GEM_EPHEM_CMD="lxc-copy"
fi
if [ "$GEM_EPHEM_NAME" = "" ]; then
    GEM_EPHEM_NAME="ubuntu16-lxc-eph"
fi

LXC_VER=$(lxc-ls --version | cut -d '.' -f 1)

if [ $LXC_VER -lt 2 ]; then
    echo "LXC >= 2.0.0 is required." >&2
    echo "Hint: LXC 2.0 is available for Trusty from backports."
    exit 1
fi

LXC_TERM="lxc-stop -t 10"
LXC_KILL="lxc-stop -k"

GEM_EPHEM_EXE="${GEM_EPHEM_CMD} -n ${GEM_EPHEM_NAME} -e"

NL="
"
TB="	"

#
#  functions
sig_hand () {
    trap ERR
    echo "signal trapped"
    if [ "$lxc_name" != "" ]; then
        set +e
        scp "${lxc_ip}:ssh.log" "out_${BUILD_UBUVER}/ssh.history"
        echo "Destroying [$lxc_name] lxc"
        sudo $LXC_KILL -n $lxc_name
        sudo lxc-destroy -n $lxc_name
    fi
    if [ -f /tmp/packager.eph.$$.log ]; then
        rm /tmp/packager.eph.$$.log
    fi
    exit 1
}

#
#  dep2var <dep> - converts in a proper way the name of a dependency to a variable name
#      <dep>    the name of the dependency
#
dep2var () {
    echo "$1" | sed 's/[-.]/_/g;s/\(.*\)/\U\1/g'
}

#
#  repo_id_get - retry git repo from local git remote command
repo_id_get () {
    local repo_name repo_line

    if ! repo_name="$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)"; then
        repo_line="$(git remote -vv | grep "^origin[ ${TB}]" | grep '(fetch)$')"
        if [ -z "$repo_line" ]; then
            echo "no remote repository associated with the current branch, exit 1"
            exit 1
        fi
    else
        repo_name="$(echo "$repo_name" | sed 's@/.*@@g')"

        repo_line="$(git remote -vv | grep "^${repo_name}[ ${TB}].*(fetch)\$")"
    fi

    if echo "$repo_line" | grep -q '[0-9a-z_-\.]\+@[a-z0-9_-\.]\+:'; then
        repo_id="$(echo "$repo_line" | sed "s/^[^ ${TB}]\+[ ${TB}]\+[^ ${TB}@]\+@//g;s/.git[ ${TB}]\+(fetch)$/.git/g;s@/${GEM_GIT_PACKAGE}.git@@g;s@:@/@g")"
    else
        repo_id="$(echo "$repo_line" | sed "s/^[^ ${TB}]\+[ ${TB}]\+git:\/\///g;s/.git[ ${TB}]\+(fetch)$/.git/g;s@/${GEM_GIT_PACKAGE}.git@@g")"
    fi

    echo "$repo_id"
}

#
#  mksafedir <dname> - try to create a directory and rise an alert if it already exists
#      <dname>    name of the directory to create
#
mksafedir () {
    local dname

    dname="$1"
    if [ "$GEM_ALWAYS_YES" != "true" -a -d "$dname" ]; then
        echo "$dname already exists"
        echo "press Enter to continue or CTRL+C to abort"
        read a
    fi
    rm -rf $dname
    mkdir -p $dname
}

usage () {
    local ret

    ret=$1

    echo
    echo "USAGE:"
    echo "    $0 [<-s|--serie> <precise|trusty|xenial>] [-D|--development] [-S--sources_copy] [-B|--binaries] [-U|--unsigned] [-R|--repository]    build debian source package."
    echo "       if -s is present try to produce sources for a specific ubuntu version (precise, trusty or xenial),"
    echo "           (default precise)"
    echo "       if -S is present try to copy sources to <GEM_DEB_MONOTONE>/<BUILD_UBUVER>/source directory"
    echo "       if -B is present binary package is build too."
    echo "       if -R is present update the local repository to the new current package"
    echo "       if -D is present a package with self-computed version is produced."
    echo "       if -U is present no sign are perfomed using gpg key related to the mantainer."
    echo "    $0 pkgtest <branch-name>                    run packaging tests into an ubuntu lxc environment"
    echo "    $0 devtest <branch-name>                    run development tests into an ubuntu lxc environment"
    echo
    exit $ret
}

_wait_ssh () {
    local lxc_ip="$1"

    for i in $(seq 1 20); do
        if ssh $lxc_ip "echo begin"; then
            break
        fi
        sleep 2
    done
    if [ $i -eq 20 ]; then
        return 1
    fi
}


add_local_pkg_repo () {
    local dep="$1"

    var_pfx="$(dep2var "$dep")"
    var_repo="${var_pfx}_REPO"
    var_branch="${var_pfx}_BRANCH"
    var_commit="${var_pfx}_COMMIT"
    if [ "${!var_repo}" != "" ]; then
        dep_repo="${!var_repo}"
    else
        dep_repo="$GEM_GIT_REPO"
    fi
    if [ "${!var_branch}" != "" ]; then
        dep_branch="${!var_branch}"
    else
        dep_branch="master"
    fi

    if [ "$dep_repo" = "$GEM_GIT_REPO" -a "$dep_branch" = "master" ]; then
        GEM_DEB_SERIE="master"
    else
        GEM_DEB_SERIE="devel/$(echo "$dep_repo" | sed 's@^.*://@@g;s@/@__@g;s/\./-/g')__${dep_branch}"
    fi
    from_dir="${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/python-${dep}.${!var_commit:0:7}"
    time_start="$(date +%s)"
    while true; do
        if scp -r "$from_dir" $lxc_ip:repo/python-${dep}; then
            break
        fi
        if [ "$dep_branch" = "$branch" ]; then
            # NOTE: currently we retry for 1 hour to get the correct dep version
            # if there is concordance between package and dependency branches
            time_cur="$(date +%s)"
            if [ $time_cur -gt $((time_start + 3600)) ]; then
                return 1
            fi
            sleep 10
        else
            # NOTE: in the other case dep branch is 'master' and package branch isn't
            #       so we try to get the correct commit package and if it isn't yet built
            #       it fallback to the latest builded
            from_dir="$(ls -drt ${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/python-${dep}* | tail -n 1)"
            scp -r "$from_dir" $lxc_ip:repo/python-${dep}
            break
        fi
    done
    ssh $lxc_ip "sudo apt-add-repository \"deb file:/home/ubuntu/repo/python-${dep} ./\""
    ssh $lxc_ip "sudo apt-get update"
}

_pkgbuild_innervm_run () {
    local lxc_ip="$1"
    local DPBP_FLAG="$2"

    trap 'local LASTERR="$?" ; trap ERR ; (exit $LASTERR) ; return' ERR

    ssh $lxc_ip mkdir build-deb
    scp -r * $lxc_ip:build-deb
    gpg -a --export | ssh $lxc_ip "sudo apt-key add -"
    ssh $lxc_ip sudo apt-get update
    ssh $lxc_ip sudo apt-get -y upgrade
    ssh $lxc_ip sudo apt-get -y install build-essential dpatch fakeroot devscripts equivs lintian quilt
    ssh $lxc_ip "sudo mk-build-deps --install --tool 'apt-get -y' build-deb/debian/control"

    ssh $lxc_ip "cd build-deb && dpkg-buildpackage $DPBP_FLAG"
    scp $lxc_ip:*.{tar.*z,changes,dsc} ../
    if echo "$DPBP_FLAG" | grep -q -v -- '-S'; then
        scp $lxc_ip:*.deb ../
    fi

    return
}

_devtest_innervm_run () {
    local i old_ifs lxc_ip="$1"

    trap 'local LASTERR="$?" ; trap ERR ; (exit $LASTERR) ; return' ERR

    ssh $lxc_ip "sudo apt-get update"
    ssh $lxc_ip "sudo apt-get upgrade -y"

    gpg -a --export | ssh $lxc_ip "sudo apt-key add -"
    # install package to manage repository properly
    ssh $lxc_ip "sudo apt-get install -y python-software-properties"

    # add custom packages
    ssh $lxc_ip mkdir -p "repo"
    scp -r ${GEM_DEB_REPO}/custom_pkgs $lxc_ip:repo/custom_pkgs
    ssh $lxc_ip "sudo apt-add-repository \"deb file:/home/ubuntu/repo/custom_pkgs ${BUILD_UBUVER} main\""

    ssh $lxc_ip "sudo apt-get update"
    ssh $lxc_ip "sudo apt-get upgrade -y"

    if [ -f _jenkins_deps_info ]; then
        source _jenkins_deps_info
    fi

    old_ifs="$IFS"
    IFS=" "
    for dep_item in $GEM_DEPENDS; do
        dep="$(echo "$dep_item" | cut -d '|' -f 1)"
        dep_type="$(echo "$dep_item" | cut -d '|' -f 2)"

        if [ "$dep_type" = "src" ]; then
            # extract dependencies for source dependencies
            pkgs_list="$(deps_list "deprec" _jenkins_deps/$dep/debian)"
            ssh $lxc_ip "sudo apt-get install -y ${pkgs_list}"

            # install source dependencies
            cd _jenkins_deps/$dep
            git archive --prefix ${dep}/ HEAD | ssh $lxc_ip "tar xv"
            cd -
        elif [ "$dep_type" = "deb" ]; then
            # cd _jenkins_deps/$dep

            add_local_pkg_repo "$dep"
            ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y python-${dep}"
        elif [ "$dep_type" = "sub" ]; then
            ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y python-${dep}"
        else
            echo "Dep type $dep_type not supported"
            exit 1
        fi
    done
    IFS="$old_ifs"

    # extract dependencies for this package
    pkgs_list="$(deps_list all debian)"
    ssh $lxc_ip "sudo apt-get install -y ${pkgs_list}"

    # TODO: version check
    git archive --prefix ${GEM_GIT_PACKAGE}/ HEAD | ssh $lxc_ip "tar xv"

    if [ -z "$GEM_DEVTEST_SKIP_TESTS" ]; then
        if [ -n "$GEM_DEVTEST_SKIP_SLOW_TESTS" ]; then
            # skip slow tests
            skip_tests="!slow,"
        fi
        ssh $lxc_ip "cd $GEM_GIT_PACKAGE ; export PYTHONPATH=/opt/openquake/lib/python2.7/site-packages; export MPLBACKEND=Agg; /opt/openquake/bin/nosetests -v -a '${skip_tests}' --with-doctest --with-coverage --cover-package=openquake.hazardlib --with-xunit"
        scp "$lxc_ip:$GEM_GIT_PACKAGE/nosetests.xml" "out_${BUILD_UBUVER}/"
    else
        if [ -d $HOME/fake-data/$GEM_GIT_PACKAGE ]; then
            cp $HOME/fake-data/$GEM_GIT_PACKAGE/* "out_${BUILD_UBUVER}/"
        fi
    fi

    trap ERR

    return
}

_pkgtest_innervm_run () {
    local lxc_ip="$1"

    trap 'local LASTERR="$?" ; trap ERR ; (exit $LASTERR) ; return' ERR

    ssh $lxc_ip "rm -f ssh.log"
    ssh $lxc_ip "sudo apt-get update"
    ssh $lxc_ip "sudo apt-get -y upgrade"
    gpg -a --export | ssh $lxc_ip "sudo apt-key add -"
    # install package to manage repository properly
    ssh $lxc_ip "sudo apt-get install -y python-software-properties"

    # create a remote "local repo" where place $GEM_DEB_PACKAGE package
    ssh $lxc_ip mkdir -p repo/${GEM_DEB_PACKAGE}
    scp build-deb/${GEM_DEB_PACKAGE}_*.deb build-deb/${GEM_DEB_PACKAGE}_*.changes \
        build-deb/${GEM_DEB_PACKAGE}_*.dsc build-deb/${GEM_DEB_PACKAGE}_*.tar.*z \
        build-deb/Packages* build-deb/Sources*  build-deb/Release* $lxc_ip:repo/${GEM_DEB_PACKAGE}
    ssh $lxc_ip "sudo apt-add-repository \"deb file:/home/ubuntu/repo/${GEM_DEB_PACKAGE} ./\""

    if [ -f _jenkins_deps_info ]; then
        source _jenkins_deps_info
    fi

    old_ifs="$IFS"
    IFS=" $NL"
    for dep_item in $GEM_DEPENDS; do
        dep="$(echo "$dep_item" | cut -d '|' -f 1)"
        dep_type="$(echo "$dep_item" | cut -d '|' -f 2)"
        # if the deb is a subpackage we skip source check
        if [ "$dep_type" == "sub" ]; then
            continue
        fi

        add_local_pkg_repo "$dep"
    done
    IFS="$old_ifs"

    # add custom packages
    scp -r ${GEM_DEB_REPO}/custom_pkgs $lxc_ip:repo/custom_pkgs
    ssh $lxc_ip "sudo apt-add-repository \"deb file:/home/ubuntu/repo/custom_pkgs ${BUILD_UBUVER} main\""
    # ssh $lxc_ip "sudo apt-add-repository ppa:nastasi-oq/test"

    ssh $lxc_ip "sudo apt-get update"
    ssh $lxc_ip "sudo apt-get upgrade -y"

    # packaging related tests (install, remove, purge, install, reinstall)
    ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y ${GEM_DEB_PACKAGE}"
    ssh $lxc_ip "sudo apt-get remove -y ${GEM_DEB_PACKAGE}"
    ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y ${GEM_DEB_PACKAGE}"
    ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES --reinstall -y ${GEM_DEB_PACKAGE}"

    scp -r "$lxc_ip://usr/share/doc/${GEM_DEB_PACKAGE}/changelog*" .
    scp -r "$lxc_ip://usr/share/doc/${GEM_DEB_PACKAGE}/README*" .

    trap ERR

    return
}

_builddoc_innervm_run () {
    local i lxc_ip="$1"

    trap 'local LASTERR="$?" ; trap ERR ; (exit $LASTERR) ; return' ERR

    ssh $lxc_ip "rm -f ssh.log"

    ssh $lxc_ip "sudo apt-get update"
    ssh $lxc_ip "sudo apt-get upgrade -y"
    gpg -a --export | ssh $lxc_ip "sudo apt-key add -"
    # install package to manage repository properly
    # ssh $lxc_ip "sudo apt-get install -y python-software-properties"

    if [ -f _jenkins_deps_info ]; then
        source _jenkins_deps_info
    fi

    ssh $lxc_ip mkdir -p "repo"

    old_ifs="$IFS"
    IFS=" "
    for dep_item in $GEM_DEPENDS; do
        dep="$(echo "$dep_item" | cut -d '|' -f 1)"
        dep_type="$(echo "$dep_item" | cut -d '|' -f 2)"

        if [ "$dep_type" = "src" ]; then
            # extract dependencies for source dependencies
            pkgs_list="$(deps_list "build" _jenkins_deps/$dep/debian)"
            ssh $lxc_ip "sudo apt-get install -y ${pkgs_list}"

            # install source dependencies
            cd _jenkins_deps/$dep
            git archive --prefix ${dep}/ HEAD | ssh $lxc_ip "tar xv"
            cd -
        elif [ "$dep_type" = "deb" ]; then
            # cd _jenkins_deps/$dep

            add_local_pkg_repo "$dep"
            ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y python-${dep}"
        elif [ "$dep_type" = "sub" ]; then
            ssh $lxc_ip "sudo apt-get install $APT_FORCE_YES -y python-${dep}"
        else
            echo "Dep type $dep_type not supported"
            exit 1
        fi
    done
    IFS="$old_ifs"

    # extract dependencies for this package
    pkgs_list="$(deps_list all debian)"
    ssh $lxc_ip "sudo apt-get install -y ${pkgs_list}"

    # TODO: version check
    git archive --prefix ${GEM_GIT_PACKAGE}/ HEAD | ssh $lxc_ip "tar xv"

    ssh $lxc_ip "sudo apt-get -y install python-pip"
    ssh $lxc_ip "sudo pip install sphinx==1.3.4"

    ssh $lxc_ip "cd ${GEM_GIT_PACKAGE} ; export PYTHONPATH=\$PWD:/opt/openquake/lib/python2.7/site-packages ; export MPLBACKEND=Agg ; cd doc/sphinx ; make html"
    scp -r "$lxc_ip:${GEM_GIT_PACKAGE}/doc/sphinx/build/html" "out_${BUILD_UBUVER}/"

    trap ERR

    return
}

#
#  deps_list <listtype> <filename> - retrieve dependencies list from debian/control and debian/rules
#                                    to be able to install them without the package
#      listtype    inform deps_list which control lines use to get dependencies
#      filename    control file used for input
#
deps_list() {
    local old_ifs out_list skip i d listtype="$1" control_file="$2"/control rules_file="$2"/rules

    if grep -q "^${BUILD_UBUVER^^}_DEP" $rules_file; then
        # Use custom dependencies in debian/rules
        rules_dep=$(grep "^${BUILD_UBUVER^^}_DEP *= *" $rules_file | sed 's/([^)]*)//g' | sed 's/^.*= *//g')
        rules_rec=$(grep "^${BUILD_UBUVER^^}_REC *= *" $rules_file | sed 's/([^)]*)//g' | sed 's/^.*= *//g')
    else
        # Otherwise use the default values in debian/rules
        rules_dep=$(grep "^DEFAULT_DEP *= *" $rules_file | sed 's/([^)]*)//g' | sed 's/^.*= *//g')
        rules_rec=$(grep "^DEFAULT_REC *= *" $rules_file | sed 's/([^)]*)//g' | sed 's/^.*= *//g')
    fi

    out_list=""
    if [ "$listtype" = "all" ]; then
        in_list="$((cat "$control_file" | egrep '^Depends:|^Recommends:|Build-Depends:' | sed 's/^\(Build-\)\?Depends://g;s/^Recommends://g' ; echo ", $rules_dep, $rules_rec") | tr '\n' ','| sed 's/,\+/,/g')"
    elif [  "$listtype" = "deprec" ]; then
        in_list="$((cat "$control_file" | egrep '^Depends:|^Recommends:' | sed 's/^Depends://g;s/^Recommends://g' ; echo ", $rules_dep, $rules_rec") | tr '\n' ','| sed 's/,\+/,/g')"
    elif [  "$listtype" = "build" ]; then
        in_list="$((cat "$control_file" | egrep '^Depends:|^Build-Depends:' | sed 's/^\(Build-\)\?Depends://g' ; echo ", $rules_dep") | tr '\n' ','| sed 's/,\+/,/g')"
    else
        in_list="$((cat "$control_file" | egrep "^Depends:" | sed 's/^Depends: //g'; echo ", $rules_dep") | tr '\n' ','| sed 's/,\+/,/g')"
    fi

    old_ifs="$IFS"
    IFS=','
    for i in $in_list ; do
        item="$(echo "$i" |  sed 's/^ \+//g;s/ \+$//g')"
        pkg_name="$(echo "${item} " | cut -d ' ' -f 1)"
        pkg_vers="$(echo "${item} " | cut -d ' ' -f 2)"
        echo "[$pkg_name][$pkg_vers]" >&2
        if echo "$pkg_name" | grep -q "^\${" ; then
            continue
        fi
        skip=0
        for d_item in $(echo "$GEM_DEPENDS" | sed 's/ /,/g'); do
            d="$(echo "$d_item" | cut -d '|' -f 1)"
            d_type="$(echo "$d_item" | cut -d '|' -f 2)"

            if [ "$d_type" != "src" ]; then
                continue
            fi
            if [ "$pkg_name" = "python-${d}" ]; then
                skip=1
                break
            fi
        done
        if [ $skip -eq 1 ]; then
            continue
        fi

        if [ "$out_list" = "" ]; then
            out_list="$pkg_name"
        else
            out_list="$out_list $pkg_name"
        fi
    done
    IFS="$old_ifs"

    echo "$out_list"

    return 0
}


_lxc_name_and_ip_get()
{
    local filename="$1" i e

    i=-1
    e=-1
    for i in $(seq 1 40); do
        if grep -q " as clone of $GEM_EPHEM_NAME" $filename 2>&1 ; then
            lxc_name="$(grep " as clone of $GEM_EPHEM_NAME" $filename | tail -n 1 | sed "s/Created \(.*\) as clone of ${GEM_EPHEM_NAME}/\1/g")"
            break
        else
            sleep 2
        fi
    done
    if [ $i -eq 40 ]; then
        return 1
    fi

    for e in $(seq 1 40); do
        sleep 2
        lxc_ip="$(sudo lxc-ls -f --filter "^${lxc_name}\$" | tail -n 1 | sed 's/ \+/ /g' | cut -d ' ' -f 5)"
        if [ "$lxc_ip" -a "$lxc_ip" != "-" ]; then
            lxc_ssh="${GEM_EPHEM_USER}@${lxc_ip}"
            break
        fi
    done
    if [ $e -eq 40 ]; then
        return 1
    fi
    echo "SUCCESSFULLY STARTED: $lxc_name ($lxc_ip)"

    return 0
}

deps_check_or_clone () {
    local dep="$1" repo="$2" branch="$3"
    local local_repo local_branch

    if [ -d _jenkins_deps/$dep ]; then
        cd _jenkins_deps/$dep
        local_repo="$(git remote -v | head -n 1 | sed 's/origin[ 	]\+//;s/ .*//g')"
        if [ "$local_repo" != "$repo" ]; then
            echo "Dependency $dep: cached repository version differs from required ('$local_repo' != '$repo')."
            exit 1
        fi
        local_branch="$(git branch 2>/dev/null | sed -e '/^[^*]/d' | sed -e 's/* \(.*\)/\1/')"
        if [ "$local_branch" != "$branch" ]; then
            echo "Dependency $dep: cached branch version differs from required ('$local_branch' != '$branch')."
            exit 1
        fi
        git clean -dfx
        cd -
    else
        git clone --depth=1 -b $branch $repo _jenkins_deps/$dep
    fi
}

devtest_run () {
    local deps old_ifs branch="$1" branch_cur

    if [ ! -d "out_${BUILD_UBUVER}" ]; then
        mkdir "out_${BUILD_UBUVER}"
    fi

    if [ ! -d _jenkins_deps ]; then
        mkdir _jenkins_deps
    fi

    #
    #  dependencies repos
    #
    # in test sources different repositories and branches can be tested
    # consistently: for each openquake dependency it try to use
    # the same repository and the same branch OR the gem repository
    # and the same branch OR the gem repository and the "master" branch
    #
    repo_id="$(repo_id_get)"
    if [ "$repo_id" != "$GEM_GIT_REPO" ]; then
        repos="git://${repo_id} ${GEM_GIT_REPO}"
    else
        repos="${GEM_GIT_REPO}"
    fi
    old_ifs="$IFS"
    IFS=" "
    for dep_item in $GEM_DEPENDS; do
        dep="$(echo "$dep_item" | cut -d '|' -f 1)"
        dep_type="$(echo "$dep_item" | cut -d '|' -f 2)"
        # if the deb is a subpackage we skip source check
        if [ "$dep_type" == "sub" ]; then
            continue
        fi
        found=0
        branch_cur="$branch"
        for repo in $repos; do
            # search of same branch in same repo or in GEM_GIT_REPO repo
            if git ls-remote --heads $repo/${dep}.git | grep -q "refs/heads/$branch_cur" ; then
                deps_check_or_clone "$dep" "$repo/${dep}.git" "$branch_cur"
                found=1
                break
            fi
        done
        # if not found it fallback in master branch of GEM_GIT_REPO repo
        if [ $found -eq 0 ]; then
            branch_cur="master"
            deps_check_or_clone "$dep" "$repo/${dep}.git" "$branch_cur"
        fi
        cd _jenkins_deps/$dep
        commit="$(git log -1 | grep '^commit' | sed 's/^commit //g')"
        cd -
        echo "dependency: $dep"
        echo "repo:       $repo"
        echo "branch:     $branch_cur"
        echo "commit:     $commit"
        echo
        var_pfx="$(dep2var "$dep")"
        if [ ! -f _jenkins_deps_info ]; then
            touch _jenkins_deps_info
        fi
        if grep -q "^${var_pfx}_COMMIT=" _jenkins_deps_info; then
            if ! grep -q "^${var_pfx}_COMMIT=$commit" _jenkins_deps_info; then
                echo "ERROR: $repo -> $branch_cur changed during test:"
                echo "before:"
                grep "^${var_pfx}_COMMIT=" _jenkins_deps_info
                echo "after:"
                echo "${var_pfx}_COMMIT=$commit"
                exit 1
            fi
        else
            echo "${var_pfx}_COMMIT=$commit" >> _jenkins_deps_info
            echo "${var_pfx}_REPO=$repo"     >> _jenkins_deps_info
            echo "${var_pfx}_BRANCH=$branch_cur" >> _jenkins_deps_info
            echo "${var_pfx}_TYPE=$dep_type" >> _jenkins_deps_info
        fi
    done
    IFS="$old_ifs"

    sudo echo
    sudo ${GEM_EPHEM_EXE} 2>&1 | tee /tmp/packager.eph.$$.log &
    _lxc_name_and_ip_get /tmp/packager.eph.$$.log

    _wait_ssh $lxc_ip

    set +e
    _devtest_innervm_run $lxc_ip
    inner_ret=$?
    sudo $LXC_TERM -n $lxc_name
    set -e

    if [ -f /tmp/packager.eph.$$.log ]; then
        rm /tmp/packager.eph.$$.log
    fi

    return $inner_ret
}


pkgtest_run () {
    local i e branch_id="$1" commit

    commit="$(git log --pretty='format:%h' -1)"

    #
    #  run build of package
    if [ -d build-deb ]; then
        if [ ! -f build-deb/${GEM_DEB_PACKAGE}_*.deb ]; then
            echo "'build-deb' directory already exists but .deb file package was not found"
            return 1

        fi
    else
        $0 $BUILD_FLAGS
    fi

    #
    #  prepare repo and install $GEM_DEB_PACKAGE package
    cd build-deb
    dpkg-scanpackages . /dev/null >Packages
    cat Packages | gzip -9c > Packages.gz
    dpkg-scansources . > Sources
    cat Sources | gzip > Sources.gz
    cat > Release <<EOF
Origin: openquake-${BUILD_UBUVER}
Label: OpenQuake Local Ubuntu Repository
Codename: $BUILD_UBUVER
Date: $(date -R)
Architectures: amd64
Components: main
Description: OpenQuake Local Ubuntu Repository
SHA256:
EOF
    printf ' '$(sha256sum Packages | cut --delimiter=' ' --fields=1)' %16d Packages\n' \
        $(wc --bytes Packages | cut --delimiter=' ' --fields=1) >> Release
    printf ' '$(sha256sum Packages.gz | cut --delimiter=' ' --fields=1)' %16d Packages.gz\n' \
        $(wc --bytes Packages.gz | cut --delimiter=' ' --fields=1) >> Release
    printf ' '$(sha256sum Sources | cut --delimiter=' ' --fields=1)' %16d Sources\n' \
        $(wc --bytes Sources | cut --delimiter=' ' --fields=1) >> Release
    printf ' '$(sha256sum Sources.gz | cut --delimiter=' ' --fields=1)' %16d Sources.gz\n' \
        $(wc --bytes Sources.gz | cut --delimiter=' ' --fields=1) >> Release
    gpg --armor --detach-sign --output Release.gpg Release
    cd -

    sudo echo
    sudo ${GEM_EPHEM_EXE} 2>&1 | tee /tmp/packager.eph.$$.log &
    _lxc_name_and_ip_get /tmp/packager.eph.$$.log

    _wait_ssh $lxc_ip

    set +e
    _pkgtest_innervm_run $lxc_ip
    inner_ret=$?
    sudo $LXC_TERM -n $lxc_name
    set -e

    if [ -f /tmp/packager.eph.$$.log ]; then
        rm /tmp/packager.eph.$$.log
    fi

    if [ $inner_ret -ne 0 ]; then
        return $inner_ret
    fi

    if [ $BUILD_REPOSITORY -eq 1 -a -d "${GEM_DEB_REPO}" ]; then
        if [ "$branch_id" != "" ]; then
            repo_id="$(repo_id_get)"
            if [ "git://$repo_id" != "$GEM_GIT_REPO" -o "$branch_id" != "master" ]; then
                CUSTOM_SERIE="devel/$(echo "$repo_id" | sed "s@/@__@g;s/\./-/g")__${branch_id}"
                if [ "$CUSTOM_SERIE" != "" ]; then
                    GEM_DEB_SERIE="$CUSTOM_SERIE"
                fi
            fi
        fi
        mkdir -p "${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}"
        repo_tmpdir="$(mktemp -d "${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/${GEM_DEB_PACKAGE}.${commit}.XXXXXX")"

        # if the monotone directory exists and is the "gem" repo and is the "master" branch then ...
        if [ -d "${GEM_DEB_MONOTONE}/${BUILD_UBUVER}/binary" ]; then
            if [ "git://$repo_id" == "$GEM_GIT_REPO" -a "$branch_id" == "master" ]; then
                cp build-deb/${GEM_DEB_PACKAGE}_*.deb build-deb/${GEM_DEB_PACKAGE}_*.changes \
                    build-deb/${GEM_DEB_PACKAGE}_*.dsc build-deb/${GEM_DEB_PACKAGE}_*.tar.*z \
                    "${GEM_DEB_MONOTONE}/${BUILD_UBUVER}/binary"
            fi
        fi

        cp build-deb/${GEM_DEB_PACKAGE}_*.deb build-deb/${GEM_DEB_PACKAGE}_*.changes \
            build-deb/${GEM_DEB_PACKAGE}_*.dsc build-deb/${GEM_DEB_PACKAGE}_*.tar.*z \
            build-deb/Packages* build-deb/Sources* build-deb/Release* "${repo_tmpdir}"
        if [ "${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/${GEM_DEB_PACKAGE}.${commit}" ]; then
            rm -rf "${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/${GEM_DEB_PACKAGE}.${commit}"
        fi
        mv "${repo_tmpdir}" "${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/${GEM_DEB_PACKAGE}.${commit}"
        echo "The package is saved here: ${GEM_DEB_REPO}/${BUILD_UBUVER}/${GEM_DEB_SERIE}/${GEM_DEB_PACKAGE}.${commit}"
    fi

    # TODO
    # app related tests (run demos)

    return
}

builddoc_run () {
    if [ ! -d "out_${BUILD_UBUVER}" ]; then
        mkdir "out_${BUILD_UBUVER}"
    fi

    sudo echo
    sudo ${GEM_EPHEM_EXE} 2>&1 | tee /tmp/packager.eph.$$.log &
    _lxc_name_and_ip_get /tmp/packager.eph.$$.log

    _wait_ssh $lxc_ip

    set +e
    _builddoc_innervm_run $lxc_ip
    inner_ret=$?
    sudo $LXC_TERM -n $lxc_name
    set -e

    if [ -f /tmp/packager.eph.$$.log ]; then
        rm /tmp/packager.eph.$$.log
    fi

    return $inner_ret
}

#
#  MAIN
#
BUILD_SOURCES_COPY=0
BUILD_BINARIES=0
BUILD_REPOSITORY=0
BUILD_DEVEL=0
BUILD_UNSIGN=0
BUILD_UBUVER_REFERENCE="precise"
BUILD_UBUVER="$BUILD_UBUVER_REFERENCE"
BUILD_ON_LXC=0
BUILD_FLAGS=""

trap sig_hand SIGINT SIGTERM
#  args management
while [ $# -gt 0 ]; do
    case $1 in
        -D|--development)
            BUILD_DEVEL=1
            if [ "$DEBFULLNAME" = "" -o "$DEBEMAIL" = "" ]; then
                echo
                echo "ERROR: set DEBFULLNAME and DEBEMAIL environment vars and run again the script"
                echo
                exit 1
            fi
            ;;
        -s|--serie)
            BUILD_UBUVER="$2"
            if [ "$BUILD_UBUVER" != "precise" -a "$BUILD_UBUVER" != "trusty" -a "$BUILD_UBUVER" != "xenial" ]; then
                echo
                echo "ERROR: ubuntu version '$BUILD_UBUVER' not supported"
                echo
                exit 1
            fi
            BUILD_FLAGS="$BUILD_FLAGS $1"
            shift
            ;;
        -S|--sources_copy)
            BUILD_SOURCES_COPY=1
            ;;
        -B|--binaries)
            BUILD_BINARIES=1
            ;;
        -R|--repository)
            BUILD_REPOSITORY=1
            ;;
        -U|--unsigned)
            BUILD_UNSIGN=1
            APT_FORCE_YES="--force-yes"
            ;;
        -L|--lxc_build)
            BUILD_ON_LXC=1
            ;;
        -h|--help)
            usage 0
            break
            ;;
        devtest)
            # Sed removes 'origin/' from the branch name
            devtest_run $(echo "$2" | sed 's@.*/@@g')
            exit $?
            break
            ;;
        pkgtest)
            # Sed removes 'origin/' from the branch name
            pkgtest_run $(echo "$2" | sed 's@.*/@@g')
            exit $?
            break
            ;;
        builddoc)
            builddoc_run $(echo "$2" | sed 's@.*/@@g')
            exit $?
            break
            ;;
        *)
            usage 1
            break
            ;;
    esac
    BUILD_FLAGS="$BUILD_FLAGS $1"
    shift
done

DPBP_FLAG=""
if [ $BUILD_BINARIES -eq 0 ]; then
    DPBP_FLAG="-S"
fi
if [ $BUILD_UNSIGN -eq 1 ]; then
    DPBP_FLAG="$DPBP_FLAG -us -uc"
fi

mksafedir "$GEM_BUILD_ROOT"
mksafedir "$GEM_BUILD_SRC"

git archive HEAD | (cd "$GEM_BUILD_SRC" ; tar xv)

# NOTE: if in the future we need modules we need to execute the following commands
#
# git submodule init
# git submodule update
##  "submodule foreach" vars: $name, $path, $sha1 and $toplevel:
# git submodule foreach "git archive HEAD | (cd \"\${toplevel}/${GEM_BUILD_SRC}/\$path\" ; tar xv ) "

# date
if [ -f gem_date_file ]; then
    dt="$(cat gem_date_file)"
else
    dt="$(date +%s)"
    echo "$dt" > gem_date_file
fi

cd "$GEM_BUILD_SRC"

# version info from openquake/hazardlib/__init__.py
ini_vers="$(cat openquake/hazardlib/__init__.py | sed -n "s/^__version__[  ]*=[    ]*['\"]\([^'\"]\+\)['\"].*/\1/gp")"
ini_maj="$(echo "$ini_vers" | sed -n 's/^\([0-9]\+\).*/\1/gp')"
ini_min="$(echo "$ini_vers" | sed -n 's/^[0-9]\+\.\([0-9]\+\).*/\1/gp')"
ini_bfx="$(echo "$ini_vers" | sed -n 's/^[0-9]\+\.[0-9]\+\.\([0-9]\+\).*/\1/gp')"
ini_suf="" # currently not included into the version array structure

# version info from debian/changelog
h="$(grep "^$GEM_DEB_PACKAGE" debian/changelog | head -n 1)"

# is it the first item of changelog ?
h_first="$(cat debian/changelog | head -n 1)"
h_is_first=0
if [ "$h" = "$h_first" ]; then
    h_is_first=1
fi

# pkg_vers="$(echo "$h" | cut -d ' ' -f 2 | cut -d '(' -f 2 | cut -d ')' -f 1 | sed -n 's/[-+].*//gp')"
pkg_name="$(echo "$h" | cut -d ' ' -f 1)"
pkg_vers="$(echo "$h" | cut -d ' ' -f 2 | cut -d '(' -f 2 | cut -d ')' -f 1)"
pkg_rest="$(echo "$h" | cut -d ' ' -f 3-)"
pkg_maj="$(echo "$pkg_vers" | sed -n 's/^\([0-9]\+\).*/\1/gp')"
pkg_min="$(echo "$pkg_vers" | sed -n 's/^[0-9]\+\.\([0-9]\+\).*/\1/gp')"
pkg_bfx="$(echo "$pkg_vers" | sed -n 's/^[0-9]\+\.[0-9]\+\.\([0-9]\+\).*/\1/gp')"
pkg_deb="$(echo "$pkg_vers" | sed -n 's/^[0-9]\+\.[0-9]\+\.[0-9]\+\(-[^+]\+\).*/\1/gp')"
pkg_suf="$(echo "$pkg_vers" | sed -n 's/^[0-9]\+\.[0-9]\+\.[0-9]\+-[^+]\+\(+.*\)/\1/gp')"
# echo "pkg [$pkg_vers] [$pkg_maj] [$pkg_min] [$pkg_bfx] [$pkg_deb] [$pkg_suf]"

if [ $BUILD_DEVEL -eq 1 ]; then
    hash="$(git log --pretty='format:%h' -1)"
    mv debian/changelog debian/changelog.orig
    cp debian/control debian/control.orig
    for dep_item in $GEM_DEPENDS; do
        dep="$(echo "$dep_item" | cut -d '|' -f 1)"
        if [ "$dep" = "oq-libs" ]; then
            sed -i "s/\(python-${dep}\) \(([<>= ]\+\)\([^)]\+\)\()\)/\1 \2\3dev0\4/g"  debian/control
        else
            sed -i "s/\(python-${dep}\) \(([<>= ]\+\)\([^)]\+\)\()\)/\1 \2\3${BUILD_UBUVER}01~dev0\4/g"  debian/control
        fi
    done

    if [ "$pkg_maj" = "$ini_maj" -a "$pkg_min" = "$ini_min" -a \
         "$pkg_bfx" = "$ini_bfx" -a "$pkg_deb" != "" ]; then
        deb_ct="$(echo "$pkg_deb" | sed 's/^-//g;s/~.*//g')"
        if [ $h_is_first -eq 1 ]; then
            pkg_deb="-$(( deb_ct ))"
        else
            pkg_deb="-$(( deb_ct + 1))"
        fi
    else
        pkg_maj="$ini_maj"
        pkg_min="$ini_min"
        pkg_bfx="$ini_bfx"
        pkg_deb="-0"
    fi

    (
      echo "$pkg_name (${pkg_maj}.${pkg_min}.${pkg_bfx}${pkg_deb}~${BUILD_UBUVER}01~dev${dt}+${hash}) ${BUILD_UBUVER}; urgency=low"
      echo
      echo "  [Automatic Script]"
      echo "  * Development version from $hash commit"
      echo
      cat debian/changelog.orig | sed -n "/^$GEM_DEB_PACKAGE/q;p"
      echo " -- $DEBFULLNAME <$DEBEMAIL>  $(date -d@$dt -R)"
      echo
    )  > debian/changelog
    cat debian/changelog.orig | sed -n "/^$GEM_DEB_PACKAGE/,\$ p" >> debian/changelog
    rm debian/changelog.orig

    sed -i "s/^__version__[  ]*=.*/__version__ = '${pkg_maj}.${pkg_min}.${pkg_bfx}${pkg_deb}~dev${dt}-${hash}'/g" openquake/hazardlib/__init__.py
else
    cp debian/changelog debian/changelog.orig
    cat debian/changelog.orig | sed "1 s/${BUILD_UBUVER_REFERENCE}/${BUILD_UBUVER}/g" > debian/changelog
    rm debian/changelog.orig
fi

if [  "$ini_maj" != "$pkg_maj" -o \
      "$ini_min" != "$pkg_min" -o \
      "$ini_bfx" != "$pkg_bfx" ]; then
    echo
    echo "Versions are not aligned"
    echo "    init:  ${ini_maj}.${ini_min}.${ini_bfx}"
    echo "    pkg:   ${pkg_maj}.${pkg_min}.${pkg_bfx}"
    echo
    echo "press [enter] to continue, [ctrl+c] to abort"
    read a
fi

# the following unexecuted block of code is a flag to identify where and how modifications can
# be performed from sources to package
if [ 0 -eq 1 ]; then
    sed -i "s/^\([ 	]*\)[^)]*\()  # release date .*\)/\1${dt}\2/g" openquake/__init__.py

    # mods pre-packaging
    mv LICENSE         openquake
    mv README.md       openquake/README
    mv celeryconfig.py openquake
    mv openquake.cfg   openquake

    mv bin/openquake   bin/oqscript.py
    mv bin             openquake/bin

    rm -rf $(find demos -mindepth 1 -maxdepth 1 | egrep -v 'demos/simple_fault_demo_hazard|demos/event_based_hazard|demos/_site_model')
fi

if [ $BUILD_ON_LXC -eq 1 ]; then
    sudo ${GEM_EPHEM_EXE} 2>&1 | tee /tmp/packager.eph.$$.log &
    _lxc_name_and_ip_get /tmp/packager.eph.$$.log
    _wait_ssh $lxc_ip

    set +e
    _pkgbuild_innervm_run $lxc_ip "$DPBP_FLAG"
    inner_ret=$?
    sudo $LXC_TERM -n $lxc_name
    set -e
    if [ -f /tmp/packager.eph.$$.log ]; then
        rm /tmp/packager.eph.$$.log
    fi
    if [ $inner_ret -ne 0 ]; then
        exit 1
    fi
else
    dpkg-buildpackage $DPBP_FLAG
fi
cd -

# if the monotone directory exists and is the "gem" repo and is the "master" branch then ...
if [ -d "${GEM_DEB_MONOTONE}/${BUILD_UBUVER}/source" -a $BUILD_SOURCES_COPY -eq 1 ]; then
    cp build-deb/${GEM_DEB_PACKAGE}_*.changes \
        build-deb/${GEM_DEB_PACKAGE}_*.dsc build-deb/${GEM_DEB_PACKAGE}_*.tar.gz \
        "${GEM_DEB_MONOTONE}/${BUILD_UBUVER}/source"
fi

if [ $BUILD_DEVEL -ne 1 ]; then
    exit 0
fi

#
# DEVEL EXTRACTION OF SOURCES
if [ -z "$GEM_SRC_PKG" ]; then
    echo "env var GEM_SRC_PKG not set, exit"
    exit 0
fi
GEM_BUILD_PKG="${GEM_SRC_PKG}/pkg"
mksafedir "$GEM_BUILD_PKG"
GEM_BUILD_EXTR="${GEM_SRC_PKG}/extr"
mksafedir "$GEM_BUILD_EXTR"
cp  ${GEM_BUILD_ROOT}/${GEM_DEB_PACKAGE}_*.deb  $GEM_BUILD_PKG
cd "$GEM_BUILD_EXTR"
dpkg -x $GEM_BUILD_PKG/${GEM_DEB_PACKAGE}_*.deb .
dpkg -e $GEM_BUILD_PKG/${GEM_DEB_PACKAGE}_*.deb
